import os, json, uuid, logging, sys
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_file, abort)
from dotenv import load_dotenv
import io

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()

try:
    import db
    logger.info("db module imported successfully")
except Exception as e:
    logger.error("Failed to import db module: %s", e, exc_info=True)
    print(f"[app] FATAL: could not import db: {e}", file=sys.stderr, flush=True)
    db = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tgm-apphub-secret-2026")

UNIVERSITIES = db.load_universities() if db is not None else []
STATUSES     = ["Not Checked", "In Progress", "Submitted"]
MONTHS       = ["January", "May", "September"]

# ── Auth decorator ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session or session["user"]["role"] != "Admin":
            flash("Access denied.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ── Group duplicate app rows by student ────────────────────────────────────────
def _group_by_student(apps):
    """Merge multiple application rows for the same student into one entry."""
    seen = {}
    result = []
    for app in apps:
        email = app.get('student_email', '').lower().strip()
        name  = app.get('student_name', '').lower().strip()
        key   = email if email else name
        schools = db.parse_schools(db.clean(app.get('schools', '')))
        if key not in seen:
            entry = dict(app)
            entry['_schools'] = list(schools)
            seen[key] = len(result)
            result.append(entry)
        else:
            idx = seen[key]
            existing = {(s.get('university',''), s.get('course',''), s.get('intake',''))
                        for s in result[idx]['_schools']}
            for s in schools:
                k = (s.get('university',''), s.get('course',''), s.get('intake',''))
                if k not in existing:
                    result[idx]['_schools'].append(s)
                    existing.add(k)
    return result

# ── Context processor ──────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    user = session.get("user")
    unread = 0
    if user:
        is_officer = user["role"] in ("Application Officer","Admin")
        unread = db.unread_count(user["name"], is_officer=is_officer)
    return {"current_user": user, "unread_count": unread, "statuses": STATUSES}

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/health")
def health():
    db_status = "ok"
    if db is None:
        db_status = "unavailable: db module failed to import"
    else:
        try:
            db.get_sb()
        except Exception as e:
            db_status = f"unavailable: {e}"
    status_code = 200 if db_status == "ok" else 503
    return jsonify({"status": "ok", "db": db_status}), status_code

# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/", methods=["GET","POST"])
@app.route("/login", methods=["GET","POST"])
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email","").strip()
        password = request.form.get("password","").strip()
        ok, user = db.login_user(email, password)
        if ok:
            session["user"] = user
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if "user" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name     = request.form.get("name","").strip()
        email    = request.form.get("email","").strip()
        password = request.form.get("password","").strip()
        confirm  = request.form.get("confirm","").strip()
        role     = request.form.get("role","Counsellor")
        if not all([name,email,password,confirm]):
            flash("Please fill in all fields.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            ok, msg = db.register_user(name, email, password, role)
            if ok:
                flash("Account created! You can now sign in.", "success")
                return redirect(url_for("login"))
            flash(msg, "error")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))

# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/dashboard")
@login_required
def dashboard():
    user = session["user"]
    role = user["role"]
    if role == "Counsellor":
        apps = _group_by_student(db.load_applications(counsellor_email=user.get("email","")))
        return render_template("counsellor_dashboard.html", apps=apps,
                               parse_schools=db.parse_schools, clean=db.clean)
    else:
        if role == "Admin":
            all_apps = db.load_applications()
        else:
            all_apps = db.load_applications(officer_name=user["name"])
        total     = len(all_apps)
        not_chk   = sum(1 for a in all_apps if a["status"]=="Not Checked")
        in_prog   = sum(1 for a in all_apps if a["status"]=="In Progress")
        submitted = sum(1 for a in all_apps if a["status"]=="Submitted")
        officers  = db.get_officers() if role=="Admin" else []
        workload  = []
        if role == "Admin":
            for o in officers:
                oa = [a for a in all_apps if a.get("assigned_officer")==o]
                workload.append({"name":o,"total":len(oa),
                    "not_checked":sum(1 for a in oa if a["status"]=="Not Checked"),
                    "in_progress":sum(1 for a in oa if a["status"]=="In Progress"),
                    "submitted":sum(1 for a in oa if a["status"]=="Submitted")})
        return render_template("officer_dashboard.html",
            total=total, not_chk=not_chk, in_prog=in_prog, submitted=submitted,
            workload=workload, role=role)

# ══════════════════════════════════════════════════════════════════════════════
# COUNSELLOR — SUBMIT & MANAGE
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/submit", methods=["GET","POST"])
@login_required
def submit():
    user = session["user"]
    if user["role"] != "Counsellor":
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        sn = request.form.get("student_name","").strip()
        se = request.form.get("student_email","").strip()
        sp = request.form.get("student_phone","").strip()
        cn = request.form.get("counsellor_name","").strip()
        ce = request.form.get("counsellor_email","").strip()
        cp = request.form.get("counsellor_phone","").strip()
        notes = request.form.get("notes","").strip()
        num_schools = int(request.form.get("num_schools",1))
        schools = []
        for i in range(1, num_schools+1):
            uni    = request.form.get(f"uni_{i}","").strip()
            course = request.form.get(f"course_{i}","").strip()
            month  = request.form.get(f"month_{i}","January")
            year   = request.form.get(f"year_{i}","2026")
            if uni: schools.append({"university":uni,"course":course,"intake":f"{month} {year}"})
        if not sn or not ce or not cp or not schools:
            flash("Please fill in all required fields and at least one school.", "error")
            return render_template("submit.html", universities=UNIVERSITIES, months=MONTHS,
                                   form=request.form, num_schools=num_schools,
                                   counsellor_phone=cp)
        app_id  = str(uuid.uuid4())[:8].upper()
        officer = db.get_next_officer()
        doc_names = []
        files = request.files.getlist("documents")
        for f in files:
            if f and f.filename:
                data = f.read()
                saved = db.upload_file(app_id, f.filename, data, f.content_type or "application/octet-stream")
                doc_names.append(saved)
        summary = ", ".join([s["university"] for s in schools])
        db.save_application({
            "app_id":app_id,"student_name":sn,"student_email":se,"student_phone":sp,
            "schools":json.dumps(schools),"counsellor_name":cn,"counsellor_email":ce,
            "counsellor_phone":cp,"notes":notes,"documents":"|".join(doc_names),
            "status":"Not Checked","assigned_officer":officer,
            "submitted_at":datetime.now().strftime("%Y-%m-%d %H:%M"),
            "last_updated":datetime.now().strftime("%Y-%m-%d %H:%M"),"officer_notes":""
        })
        db.add_notification({
            "id":str(uuid.uuid4())[:8],"recipient":"ALL_OFFICERS","sender":cn,
            "type":"new_application",
            "message":f"New application from {cn} — {sn} | {summary}",
            "app_id":app_id,"time":datetime.now().strftime("%Y-%m-%d %H:%M"),"read":False
        })
        flash(f"Application submitted! ID: {app_id} — Assigned to: {officer}", "success")
        return redirect(url_for("my_applications"))
    counsellor_phone = db.get_counsellor_phone(user.get("email",""))
    return render_template("submit.html", universities=UNIVERSITIES, months=MONTHS,
                           form={}, num_schools=1, user=user,
                           counsellor_phone=counsellor_phone)

@app.route("/my-applications")
@login_required
def my_applications():
    user = session["user"]
    if user["role"] != "Counsellor":
        return redirect(url_for("dashboard"))
    apps = _group_by_student(db.load_applications(counsellor_email=user.get("email","")))
    return render_template("my_applications.html", apps=apps,
                           parse_schools=db.parse_schools, clean=db.clean)

@app.route("/edit/<app_id>", methods=["GET","POST"])
@login_required
def edit_application(app_id):
    user = session["user"]
    if user["role"] != "Counsellor":
        return redirect(url_for("dashboard"))
    app_data = db.get_application(app_id)
    if not app_data or app_data.get("counsellor_email") != user.get("email",""):
        flash("Application not found.", "error")
        return redirect(url_for("my_applications"))
    if request.method == "POST":
        sn = request.form.get("student_name","").strip()
        se = request.form.get("student_email","").strip()
        sp = request.form.get("student_phone","").strip()
        cn = request.form.get("counsellor_name","").strip()
        ce = request.form.get("counsellor_email","").strip()
        cp = request.form.get("counsellor_phone","").strip()
        notes = request.form.get("notes","").strip()
        num_schools = int(request.form.get("num_schools",1))
        schools = []
        for i in range(1, num_schools+1):
            uni    = request.form.get(f"uni_{i}","").strip()
            course = request.form.get(f"course_{i}","").strip()
            month  = request.form.get(f"month_{i}","January")
            year   = request.form.get(f"year_{i}","2026")
            if uni: schools.append({"university":uni,"course":course,"intake":f"{month} {year}"})
        existing_docs = [d for d in db.clean(app_data.get("documents","")).split("|") if d.strip()]
        files = request.files.getlist("documents")
        for f in files:
            if f and f.filename:
                data = f.read()
                saved = db.upload_file(app_id, f.filename, data, f.content_type or "application/octet-stream")
                if saved not in existing_docs: existing_docs.append(saved)
        db.edit_application(app_id, {
            "student_name":sn,"student_email":se,"student_phone":sp,
            "counsellor_name":cn,"counsellor_email":ce,"counsellor_phone":cp,
            "schools":json.dumps(schools),"notes":notes,"documents":"|".join(existing_docs)
        })
        flash("Application updated successfully.", "success")
        return redirect(url_for("my_applications"))
    schools = db.parse_schools(db.clean(app_data.get("schools","")))
    return render_template("edit_application.html", app=app_data, schools=schools,
                           universities=UNIVERSITIES, months=MONTHS, num_schools=len(schools) or 1)

# ══════════════════════════════════════════════════════════════════════════════
# OFFICER / ADMIN — APPLICATIONS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/queue")
@login_required
def queue():
    user = session["user"]
    if user["role"] not in ("Application Officer","Admin"):
        return redirect(url_for("dashboard"))
    if user["role"] == "Admin":
        apps = db.load_applications()
    else:
        apps = db.load_applications(officer_name=user["name"])
    f_status = request.args.get("status","All")
    f_search = request.args.get("search","").strip()
    f_couns  = request.args.get("counsellor","All")
    counsellors = list(set(a["counsellor_name"] for a in apps))
    filtered = apps
    if f_status != "All":  filtered = [a for a in filtered if a["status"]==f_status]
    if f_couns  != "All":  filtered = [a for a in filtered if a["counsellor_name"]==f_couns]
    if f_search:           filtered = [a for a in filtered if f_search.lower() in a["student_name"].lower()]
    return render_template("queue.html", apps=filtered, all_apps=apps,
                           f_status=f_status, f_search=f_search, f_couns=f_couns,
                           counsellors=counsellors, parse_schools=db.parse_schools,
                           clean=db.clean, get_file_url=db.get_file_url)

@app.route("/update-status", methods=["POST"])
@login_required
def update_status():
    app_id = request.form.get("app_id")
    status = request.form.get("status")
    notes  = request.form.get("notes","")
    app_data = db.get_application(app_id)
    if app_data:
        db.update_status(app_id, status, notes)
        db.add_notification({
            "id":str(uuid.uuid4())[:8],
            "recipient":app_data.get("counsellor_name",""),
            "sender":session["user"]["name"],"type":"status_update",
            "message":f"Status updated for {app_data['student_name']} — {status}" + (f" | Note: {notes}" if notes else ""),
            "app_id":app_id,"time":datetime.now().strftime("%Y-%m-%d %H:%M"),"read":False
        })
        flash(f"Status updated to {status}.", "success")
    return redirect(request.referrer or url_for("queue"))

@app.route("/reassign", methods=["POST"])
@login_required
def reassign():
    if session["user"]["role"] != "Admin":
        abort(403)
    app_id  = request.form.get("app_id")
    officer = request.form.get("officer")
    db.reassign(app_id, officer)
    app_data = db.get_application(app_id)
    if app_data:
        db.add_notification({
            "id":str(uuid.uuid4())[:8],"recipient":officer,
            "sender":session["user"]["name"],"type":"assignment",
            "message":f"Application {app_id} for {app_data['student_name']} has been assigned to you.",
            "app_id":app_id,"time":datetime.now().strftime("%Y-%m-%d %H:%M"),"read":False
        })
    flash(f"Reassigned to {officer}.", "success")
    return redirect(request.referrer or url_for("assignments"))

# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/notifications")
@login_required
def notifications():
    user = session["user"]
    notifs = db.load_notifications(recipient=user["name"])
    if user["role"] in ("Application Officer","Admin"):
        db.mark_officer_read(user["name"])
    else:
        db.mark_read(user["name"])
    combined = list({n["id"]:n for n in notifs}.values())
    sorted_n = sorted(combined, key=lambda x:x.get("time",""), reverse=True)
    return render_template("notifications.html", notifications=sorted_n)

# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/reports")
@login_required
def reports():
    user = session["user"]
    if user["role"] == "Admin":
        all_apps = db.load_applications()
    else:
        all_apps = db.load_applications(officer_name=user["name"])
    q_uni   = request.args.get("uni","").strip()
    q_month = request.args.get("month","All")
    q_year  = request.args.get("year","2026")
    results = []
    if request.args.get("query"):
        for a in all_apps:
            for s in db.parse_schools(db.clean(a.get("schools",""))):
                if (not q_uni or q_uni.lower() in s.get("university","").lower()) and \
                   (q_month=="All" or q_month in s.get("intake","")) and \
                   q_year in s.get("intake",""):
                    results.append({"App ID":db.clean(a["app_id"]),"Student":db.clean(a["student_name"]),
                        "University":s.get("university",""),"Course":s.get("course",""),
                        "Intake":s.get("intake",""),"Status":db.clean(a["status"]),
                        "Counsellor":db.clean(a["counsellor_name"]),"Officer":db.clean(a["assigned_officer"])})
    status_breakdown = {}
    officer_breakdown = {}
    counsellor_breakdown = {}
    for a in all_apps:
        s = a["status"]; status_breakdown[s] = status_breakdown.get(s,0)+1
        o = a.get("assigned_officer","Unassigned"); officer_breakdown[o] = officer_breakdown.get(o,0)+1
        c = a.get("counsellor_name","Unknown"); counsellor_breakdown[c] = counsellor_breakdown.get(c,0)+1
    return render_template("reports.html",
        all_apps=all_apps, results=results, q_uni=q_uni, q_month=q_month, q_year=q_year,
        months=MONTHS, status_breakdown=status_breakdown,
        officer_breakdown=officer_breakdown, counsellor_breakdown=counsellor_breakdown,
        role=user["role"])

@app.route("/export")
@login_required
def export():
    user = session["user"]
    if user["role"] == "Admin": all_apps = db.load_applications()
    else: all_apps = db.load_applications(officer_name=user["name"])
    rows = [{"App ID":db.clean(a["app_id"]),"Student":db.clean(a["student_name"]),
             "Email":db.clean(a.get("student_email","")),"Phone":db.clean(a.get("student_phone","")),
             "Counsellor":db.clean(a["counsellor_name"]),"Status":db.clean(a["status"]),
             "Officer":db.clean(a.get("assigned_officer","")),"Submitted":db.clean(a.get("submitted_at","")),
             "Notes":db.clean(a.get("officer_notes",""))} for a in all_apps]
    data = db.export_excel(rows)
    return send_file(io.BytesIO(data), download_name="tgm_applications.xlsx",
                     as_attachment=True, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/export-query")
@login_required
def export_query():
    user = session["user"]
    if user["role"] == "Admin": all_apps = db.load_applications()
    else: all_apps = db.load_applications(officer_name=user["name"])
    q_uni   = request.args.get("uni","").strip()
    q_month = request.args.get("month","All")
    q_year  = request.args.get("year","2026")
    results = []
    for a in all_apps:
        for s in db.parse_schools(db.clean(a.get("schools",""))):
            if (not q_uni or q_uni.lower() in s.get("university","").lower()) and \
               (q_month=="All" or q_month in s.get("intake","")) and q_year in s.get("intake",""):
                results.append({"App ID":db.clean(a["app_id"]),"Student":db.clean(a["student_name"]),
                    "University":s.get("university",""),"Course":s.get("course",""),
                    "Intake":s.get("intake",""),"Status":db.clean(a["status"]),
                    "Counsellor":db.clean(a["counsellor_name"]),"Officer":db.clean(a["assigned_officer"])})
    data = db.export_excel(results)
    return send_file(io.BytesIO(data), download_name="query_results.xlsx",
                     as_attachment=True, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — ASSIGNMENTS & USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/assignments")
@admin_required
@login_required
def assignments():
    all_apps = db.load_applications()
    officers = db.get_officers()
    workload = []
    for o in officers:
        oa = [a for a in all_apps if a.get("assigned_officer")==o]
        workload.append({"name":o,"total":len(oa),
            "not_checked":sum(1 for a in oa if a["status"]=="Not Checked"),
            "in_progress":sum(1 for a in oa if a["status"]=="In Progress"),
            "submitted":sum(1 for a in oa if a["status"]=="Submitted")})
    return render_template("assignments.html", apps=all_apps, officers=officers,
                           workload=workload, parse_schools=db.parse_schools, clean=db.clean)

@app.route("/users")
@admin_required
@login_required
def user_management():
    users = db.get_all_users()
    return render_template("user_management.html", users=users)

@app.route("/users/update-role", methods=["POST"])
@admin_required
@login_required
def update_role():
    email   = request.form.get("email")
    role    = request.form.get("role")
    new_pw  = request.form.get("new_password","").strip()
    if email == session["user"]["email"]:
        flash("You cannot change your own role.", "error")
    else:
        db.update_user_role(email, role)
        if new_pw:
            db.reset_user_password(email, new_pw)
            flash(f"Role and password updated.", "success")
        else:
            flash(f"Role updated.", "success")
    return redirect(url_for("user_management"))

@app.route("/users/delete", methods=["POST"])
@admin_required
@login_required
def delete_user():
    email = request.form.get("email")
    if email == session["user"]["email"]:
        flash("You cannot remove yourself.", "error")
    else:
        db.delete_user(email)
        flash("User removed.", "success")
    return redirect(url_for("user_management"))

@app.route("/users/add", methods=["POST"])
@admin_required
@login_required
def add_user():
    name  = request.form.get("name","").strip()
    email = request.form.get("email","").strip()
    pw    = request.form.get("password","").strip()
    role  = request.form.get("role","Counsellor")
    if all([name,email,pw]):
        ok, msg = db.register_user(name, email, pw, role)
        flash(msg, "success" if ok else "error")
    else:
        flash("Please fill in all fields.", "error")
    return redirect(url_for("user_management"))

# ── API for universities autocomplete ─────────────────────────────────────────
@app.route("/api/universities")
@login_required
def api_universities():
    q = request.args.get("q","").lower()
    results = [u for u in UNIVERSITIES if q in u.lower()][:50]
    return jsonify(results)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
