from flask import Flask, render_template, request, redirect, url_for, flash, current_app, jsonify, Response, abort
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Location, Employee, Punch, User, PunchAudit
from utils import compute_shifts
import math
import os
from dotenv import load_dotenv
from collections import defaultdict
from sqlalchemy import inspect, text
from functools import wraps
import io
import csv

TIMEZONES = {
    'Sacramento':   'America/Los_Angeles',
    'Dallas':       'America/Chicago',
    'Houston':      'America/Chicago',
    'Indianapolis': 'America/New_York'
}

app = Flask(__name__)

load_dotenv()

KIOSK_KEY = os.environ.get("KIOSK_KEY", "")

app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-secret-change-me'),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

#Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# User loader for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ----------------------------
# ✅ Guards + lightweight schema safety (no Alembic in this repo)
# ----------------------------
def manager_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_manager", False):
            flash("Manager access required.", "warning")
            return redirect(url_for("manager_login"))
        return fn(*args, **kwargs)
    return wrapper

# ----------------------------
# ✅ Role-based guards
# ----------------------------
def supervisor_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Login required.", "warning")
            return redirect(url_for("login", next=request.path))

        if getattr(current_user, "active", True) is False:
            flash("Account disabled. Contact an admin.", "danger")
            return redirect(url_for("login"))

        if not getattr(current_user, "is_supervisor", False):
            flash("Supervisor access required.", "warning")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Login required.", "warning")
            return redirect(url_for("login", next=request.path))

        if getattr(current_user, "active", True) is False:
            flash("Account disabled. Contact an admin.", "danger")
            return redirect(url_for("login"))

        if not getattr(current_user, "is_admin", False):
            flash("Admin access required.", "warning")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper

def require_user_location_scope(target_location_id: int):
    """
    ✅ Supervisors are locked to their assigned location.
    ✅ Admins can access all locations.
    """
    if getattr(current_user, "is_admin", False):
        return

    user_loc_id = getattr(current_user, "location_id", None)
    if not user_loc_id:
        flash("Your supervisor account is not assigned to a location. Contact an admin.", "danger")
        abort(403)

    if int(user_loc_id) != int(target_location_id):
        flash("Access denied: you can only manage your assigned location.", "danger")
        abort(403)

def ensure_schema():
    """
    Lightweight schema helper.
    - Adds Employee.active / Employee.terminated_at columns if missing
    - Ensures PunchAudit table exists
    """
    insp = inspect(db.engine)

    if "employees" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("employees")}

        if "active" not in cols:
            try:
                db.session.execute(text("ALTER TABLE employees ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE"))
                db.session.commit()
            except Exception:
                db.session.rollback()

        cols = {c["name"] for c in insp.get_columns("employees")}
        if "terminated_at" not in cols:
            try:
                db.session.execute(text("ALTER TABLE employees ADD COLUMN terminated_at TIMESTAMP NULL"))
                db.session.commit()
            except Exception:
                db.session.rollback()

        try:
            db.session.execute(text("UPDATE employees SET active = TRUE WHERE active IS NULL"))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Create any new tables (e.g., punch_audits)
    try:
        db.create_all()
    except Exception:
        pass
    
    # ✅ Users: add role + active if missing
    if "users" in insp.get_table_names():
        ucols = {c["name"] for c in insp.get_columns("users")}

        if "role" not in ucols:
            try:
                db.session.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'employee'"))
                db.session.commit()
            except Exception:
                db.session.rollback()

        ucols = {c["name"] for c in insp.get_columns("users")}
        if "active" not in ucols:
            try:
                db.session.execute(text("ALTER TABLE users ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE"))
                db.session.commit()
            except Exception:
                db.session.rollback()

        insp = inspect(db.engine)                

        
        # ✅ If you still have legacy is_manager values in DB, upgrade them to role=admin
        # (only runs safely if is_manager column exists)
        try:
            db.session.execute(text("UPDATE users SET role='admin' WHERE role='employee' AND is_manager=TRUE"))
            db.session.commit()
        except Exception:
            db.session.rollback()    

        # ✅ Users: add location_id if missing
        ucols = {c["name"] for c in insp.get_columns("users")}
        if "location_id" not in ucols:
            try:
                db.session.execute(text("ALTER TABLE users ADD COLUMN location_id INTEGER NULL"))
                db.session.commit()
            except Exception:
                db.session.rollback()

with app.app_context():
    db.create_all()
    ensure_schema()

    # ----------------------------
    # ✅ Bootstrap: create first admin user (one-time)
    # Controlled via ENV so you can remove/rotate safely.
    # ----------------------------
    bootstrap_user = (os.environ.get("BOOTSTRAP_ADMIN_USERNAME") or "").strip()
    bootstrap_pass = (os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or "").strip()

    if bootstrap_user and bootstrap_pass:
        existing_admin = User.query.filter_by(role="admin").first()
        if not existing_admin:
            u = User(username=bootstrap_user, role="admin", active=True, location_id=None)
            u.set_password(bootstrap_pass)
            db.session.add(u)
            db.session.commit()
            print(f"✅ Bootstrapped admin user: {bootstrap_user}")

    coords = [
        ('Sacramento',   38.535168, -121.3661184),
        ('Dallas',       32.5372008,  -96.7493993),
        ('Houston',      29.835264,  -95.5383808),
        ('Indianapolis', 39.6836058,  -86.1927711),
    ]
    for name, lat, lng in coords:
        loc = Location.query.filter_by(name=name).first()
        if loc:
            loc.lat, loc.lng = lat, lng
        else:
            db.session.add(Location(name=name, lat=lat, lng=lng))

    db.session.commit()

# Purge 5-month-old punches nightly
def purge_old():
    # when APScheduler fires, we need our own app context
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=5*30)
        Punch.query.filter(Punch.timestamp < cutoff).delete()
        db.session.commit()

@app.route('/')
@login_required
def index():
    locs = Location.query.all()
    sel  = int(request.args.get('loc', locs[0].id if locs else 1))
    emp  = request.args.get('emp', type=int)    # parse employee filter

    location = Location.query.get(sel)
    tz       = ZoneInfo(TIMEZONES[location.name])
    current_date = datetime.now(tz).strftime('%A, %B %d, %Y')

    # compute UTC window for "today"
    now_local      = datetime.now(tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_local = midnight_local + timedelta(days=1)
    start_utc = midnight_local.astimezone(ZoneInfo('UTC'))
    end_utc   = tomorrow_local.astimezone(ZoneInfo('UTC'))

    # build query
    query = (Punch.query
             .join(Employee)
             .filter(Employee.location_id==sel,
                     Punch.timestamp>=start_utc,
                     Punch.timestamp< end_utc))
    if emp:
        query = query.filter(Punch.employee_id==emp)

    raw = query.order_by(Punch.timestamp.desc()).limit(20).all()

    feed = []
    for p in raw:
        local_ts = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        feed.append({
            'time_str': local_ts.strftime('%I:%M:%S %p'),
            'employee': p.employee.name,
            'type':     p.type
        })

    # employee list (for dropdown)
    q = Employee.query.filter_by(location_id=sel).filter(Employee.active.is_(True))
    emps = q.order_by(Employee.name.asc()).all()

    return render_template(
        'index.html',
        locations=locs,
        sel=sel,
        emps=emps,
        feed=feed,
        current_date=current_date,
        emp=emp  # <-- keep this as the selected employee_id (int) from querystring
    )

@app.route('/punch', methods=['POST'])
def punch():
    loc_id = request.form.get('loc', type=int)
    emp_val = request.form.get('employee_id')
    if not emp_val:
        flash('Please select an employee before punching.', 'warning')
        return redirect(url_for('index', loc=loc_id))

    eid = int(emp_val)

    emp = Employee.query.get(eid)
    if not emp:
        flash('Employee not found.', 'danger')
        return redirect(url_for('index', loc=loc_id))
    
    # Safe check even if old model is running
    if getattr(emp, "active", True) is False:
        flash('This employee is inactive and cannot punch.', 'danger')
        return redirect(url_for('index', loc=loc_id))

    punch_type = request.form.get('type', 'IN')
    p = Punch(employee_id=eid, type=punch_type, timestamp=datetime.utcnow())
    db.session.add(p)
    db.session.commit()

    # Kiosk mode redirect (auto-reset)
    if request.form.get('kiosk') == '1':
        flash(f"{emp.name} clocked {punch_type}.", "success")
        return redirect(url_for('kiosk', loc=loc_id))
    
    return redirect(url_for('index', loc=loc_id, emp=eid))

# ----------------------------
# ✅ KIOSK MODE (no login)
# ----------------------------
@app.route('/kiosk')
def kiosk():
    # Optional kiosk protection via URL key
    if KIOSK_KEY and request.args.get("key") != KIOSK_KEY:
        return "Unauthorized", 401

    locs = Location.query.order_by(Location.name).all()
    if not locs:
        flash("No locations configured.", "danger")
        return redirect(url_for("index"))

    try:
        sel = int(request.args.get('loc', locs[0].id))
    except Exception:
        sel = locs[0].id

    location = Location.query.get(sel)
    if not location:
        sel = locs[0].id
        location = Location.query.get(sel)

    # Active employees only
    emps = (Employee.query
            .filter(Employee.location_id == sel, Employee.active.is_(True))
            .order_by(Employee.name.asc())
            .all())

    tz = ZoneInfo(TIMEZONES[location.name])
    current_date = datetime.now(tz).strftime('%A, %B %d, %Y')

    return render_template(
        'kiosk.html',
        locations=locs,
        sel=sel,
        emps=emps,
        current_date=current_date,
        kiosk_mode=True
    )

# ----------------------------
# ✅ ADMIN: User Management
# ----------------------------
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            username = (request.form.get("username") or "").strip()
            password = (request.form.get("password") or "").strip()
            role = (request.form.get("role") or "employee").strip().lower()
            location_id = request.form.get("location_id", type=int)            

            if role not in ("employee", "supervisor", "admin"):
                flash("Invalid role.", "warning")
                return redirect(url_for("admin_users"))

            if not username or not password:
                flash("Username and password are required.", "warning")
                return redirect(url_for("admin_users"))

            if User.query.filter_by(username=username).first():
                flash("Username already exists.", "warning")
                return redirect(url_for("admin_users"))

            u = User(username=username, role=role, active=True, location_id=location_id)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash("User created.", "success")
            return redirect(url_for("admin_users"))

        if action == "set_location":
            uid = int(request.form.get("user_id"))
            location_id = request.form.get("location_id", type=int)

            u = User.query.get_or_404(uid)
            u.location_id = location_id
            db.session.commit()
            flash("Location updated.", "success")
            return redirect(url_for("admin_users"))

        if action == "toggle_active":
            uid = int(request.form.get("user_id"))
            u = User.query.get_or_404(uid)

            if u.id == current_user.id:
                flash("You cannot disable your own account.", "warning")
                return redirect(url_for("admin_users"))

            u.active = not getattr(u, "active", True)
            db.session.commit()
            flash("User updated.", "success")
            return redirect(url_for("admin_users"))

        if action == "reset_password":
            uid = int(request.form.get("user_id"))
            newpw = (request.form.get("new_password") or "").strip()
            if not newpw:
                flash("Password required.", "warning")
                return redirect(url_for("admin_users"))

            u = User.query.get_or_404(uid)
            u.set_password(newpw)
            db.session.commit()
            flash("Password reset.", "success")
            return redirect(url_for("admin_users"))

        if action == "set_role":
            uid = int(request.form.get("user_id"))
            role = (request.form.get("role") or "").strip().lower()
            if role not in ("employee", "supervisor", "admin"):
                flash("Invalid role.", "warning")
                return redirect(url_for("admin_users"))

            u = User.query.get_or_404(uid)
            u.role = role
            db.session.commit()
            flash("Role updated.", "success")
            return redirect(url_for("admin_users"))

    users = User.query.order_by(User.active.desc(), User.role.asc(), User.username.asc()).all()
    locations = Location.query.order_by(Location.name.asc()).all()
    return render_template("admin_users.html", users=users, locations=locations)

# ----------------------------
# ✅ ADMIN: Punch edit + audit log
# ----------------------------
@app.route('/admin/punches')
@supervisor_required
def admin_punches():
    locations = Location.query.order_by(Location.name).all()
    if not locations:
        flash("No locations configured.", "danger")
        return redirect(url_for("index"))

    try:
        loc_id = int(request.args.get('loc', locations[0].id))
    except Exception:
        loc_id = locations[0].id

    loc = Location.query.get(loc_id)
    if not loc:
        loc = locations[0]
        loc_id = loc.id


    # ✅ Supervisors can only manage punches for their assigned location
    require_user_location_scope(loc_id)

    tz = ZoneInfo(TIMEZONES[loc.name])
    today_local = datetime.now(tz)
    days_since_monday = today_local.weekday()
    this_monday = (today_local - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).date()

    three_months_ago = today_local.date() - timedelta(days=90)
    mondays = []
    m = this_monday
    while m >= three_months_ago:
        mondays.append(m)
        m -= timedelta(days=7)
    mondays.sort(reverse=True)

    week_start_str = request.args.get('week_start')
    if week_start_str:
        try:
            candidate = datetime.fromisoformat(week_start_str).date()
            week_start_date = candidate if candidate in mondays else this_monday
        except Exception:
            week_start_date = this_monday
    else:
        week_start_date = this_monday

    week_start_local = datetime.combine(week_start_date, datetime.min.time(), tzinfo=tz)
    week_end_local   = week_start_local + timedelta(days=7)
    week_start_utc   = week_start_local.astimezone(ZoneInfo('UTC'))
    week_end_utc     = week_end_local.astimezone(ZoneInfo('UTC'))

    punches = (
        Punch.query
             .join(Employee)
             .filter(Employee.location_id == loc_id,
                     Punch.timestamp >= week_start_utc,
                     Punch.timestamp <  week_end_utc)
             .order_by(Punch.timestamp.desc())
             .all()
    )

    rows = []
    for p in punches:
        local_ts = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        rows.append({
            "id": p.id,
            "employee": p.employee.name,
            "employee_id": p.employee_id,
            "type": p.type,
            "local_str": local_ts.strftime("%Y-%m-%d %I:%M %p"),
        })

    return render_template(
        "admin_punches.html",
        locations=locations,
        loc=loc,
        mondays=mondays,
        selected_monday=week_start_date,
        rows=rows
    )


@app.route('/admin/punch/<int:punch_id>/edit', methods=['GET','POST'])
@supervisor_required
def admin_edit_punch(punch_id: int):
    p = Punch.query.get_or_404(punch_id)

    require_user_location_scope(p.employee.location_id)
    
    if request.method == 'POST':
        new_type = (request.form.get("type") or "").strip().upper()
        new_ts_str = (request.form.get("timestamp") or "").strip()
        note = (request.form.get("note") or "").strip()[:500]

        if new_type not in ("IN", "OUT"):
            flash("Invalid type.", "warning")
            return redirect(url_for("admin_edit_punch", punch_id=punch_id))

        try:
            new_local = datetime.fromisoformat(new_ts_str)
        except Exception:
            flash("Invalid timestamp.", "warning")
            return redirect(url_for("admin_edit_punch", punch_id=punch_id))

        emp_loc = Location.query.get(p.employee.location_id)
        tz = ZoneInfo(TIMEZONES[emp_loc.name])
        new_local = new_local.replace(tzinfo=tz)
        new_utc = new_local.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)

        db.session.add(PunchAudit(
            punch_id=p.id,
            employee_id=p.employee_id,
            changed_by_user_id=getattr(current_user, "id", None),
            action="EDIT",
            old_type=p.type,
            new_type=new_type,
            old_timestamp=p.timestamp,
            new_timestamp=new_utc,
            note=note or None,
        ))

        p.type = new_type
        p.timestamp = new_utc
        db.session.commit()

        flash("Punch updated (audit logged).", "success")
        return redirect(url_for("admin_punches", loc=p.employee.location_id))

    emp_loc = Location.query.get(p.employee.location_id)
    tz = ZoneInfo(TIMEZONES[emp_loc.name])
    local_ts = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
    local_value = local_ts.strftime("%Y-%m-%dT%H:%M")

    return render_template(
        "admin_edit_punch.html",
        punch=p,
        employee=p.employee,
        location=emp_loc,
        local_value=local_value
    )

@app.route('/admin/punch/<int:punch_id>/delete', methods=['POST'])
@supervisor_required
def admin_delete_punch(punch_id: int):
    p = Punch.query.get_or_404(punch_id)

    require_user_location_scope(p.employee.location_id)

    note = (request.form.get("note") or "").strip()[:500]

    # ✅ Audit log BEFORE deleting
    db.session.add(PunchAudit(
        punch_id=p.id,
        employee_id=p.employee_id,
        changed_by_user_id=getattr(current_user, "id", None),
        action="DELETE",
        old_type=p.type,
        new_type=None,
        old_timestamp=p.timestamp,
        new_timestamp=None,
        note=note or "Deleted punch",
    ))

    loc_id = p.employee.location_id  # keep for redirect after delete
    db.session.delete(p)
    db.session.commit()

    flash("Punch deleted (audit logged).", "success")
    return redirect(url_for("admin_punches", loc=loc_id))

@app.route('/admin/audit')
@supervisor_required
def admin_audit():
    q = PunchAudit.query

    # ✅ Supervisors: only see audits for their assigned location
    if not getattr(current_user, "is_admin", False):
        user_loc_id = getattr(current_user, "location_id", None)
        if not user_loc_id:
            flash("Your supervisor account is not assigned to a location. Contact an admin.", "danger")
            abort(403)

        q = (q.join(Employee, Employee.id == PunchAudit.employee_id)
               .filter(Employee.location_id == int(user_loc_id)))

    q = (q.order_by(PunchAudit.created_at.desc())
           .limit(250)
           .all())

    rows = []
    for a in q:
        emp = Employee.query.get(a.employee_id) if a.employee_id else None
        user = User.query.get(a.changed_by_user_id) if a.changed_by_user_id else None
        rows.append({
            "id": a.id,
            "created_at": a.created_at,
            "action": a.action,
            "employee": emp.name if emp else "—",
            "changed_by": user.username if user else "—",
            "old_type": a.old_type,
            "new_type": a.new_type,
            "note": a.note or ""
        })

    return render_template("admin_audit.html", rows=rows)

@app.route('/admin/payroll_export.csv')
@admin_required
def payroll_export_csv():
    locations = Location.query.order_by(Location.name).all()
    if not locations:
        return Response("No locations configured", mimetype="text/plain", status=400)

    try:
        loc_id = int(request.args.get('loc', locations[0].id))
    except Exception:
        loc_id = locations[0].id

    loc = Location.query.get(loc_id)
    if not loc:
        loc = locations[0]
        loc_id = loc.id

    tz = ZoneInfo(TIMEZONES[loc.name])
    today_local = datetime.now(tz)
    days_since_monday = today_local.weekday()
    this_monday = (today_local - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).date()

    week_start_str = request.args.get('week_start')
    if week_start_str:
        try:
            week_start_date = datetime.fromisoformat(week_start_str).date()
        except Exception:
            week_start_date = this_monday
    else:
        week_start_date = this_monday

    week_start_local = datetime.combine(week_start_date, datetime.min.time(), tzinfo=tz)
    week_end_local   = week_start_local + timedelta(days=7)
    week_start_utc   = week_start_local.astimezone(ZoneInfo('UTC'))
    week_end_utc     = week_end_local.astimezone(ZoneInfo('UTC'))

    punches = (
        Punch.query
             .join(Employee)
             .filter(Employee.location_id == loc_id,
                     Punch.timestamp >= week_start_utc,
                     Punch.timestamp <  week_end_utc)
             .order_by(Punch.employee_id, Punch.timestamp)
             .all()
    )

    def round_to_15(dt_local):
        minute = dt_local.minute
        remainder = minute % 15
        if remainder < 8:
            new_minute = minute - remainder
        else:
            new_minute = minute + (15 - remainder)
        if new_minute == 60:
            dt_local = dt_local.replace(hour=(dt_local.hour + 1) % 24, minute=0, second=0, microsecond=0)
        else:
            dt_local = dt_local.replace(minute=new_minute, second=0, microsecond=0)
        return dt_local

    by_emp = defaultdict(list)
    for p in punches:
        local_dt = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        local_dt = round_to_15(local_dt)
        by_emp[p.employee_id].append((p.type, local_dt))

    def compute_seconds(events):
        events.sort(key=lambda x: x[1])
        total = 0
        last_in = None
        for t, ts in events:
            if t == "IN":
                last_in = ts
            elif t == "OUT" and last_in:
                delta = (ts - last_in).total_seconds()
                if delta > 0:
                    total += delta
                last_in = None
        return int(total)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Location", "Week Start (Mon)", "Employee", "Total Hours (Rounded 15)", "Regular Hours", "Overtime Hours"])

    for emp in (Employee.query.filter(Employee.location_id == loc_id).order_by(Employee.name.asc()).all()):
        secs = compute_seconds(by_emp.get(emp.id, []))
        remainder = secs % 900
        secs_rounded = secs - remainder if remainder < 450 else secs + (900 - remainder)

        total_hours = round(secs_rounded / 3600, 2)
        reg = round(min(total_hours, 40.0), 2)
        ot  = round(max(total_hours - 40.0, 0.0), 2)

        # Hide terminated employees with no hours
        if total_hours == 0 and getattr(emp, "active", True) is False:
            continue

        w.writerow([loc.name, week_start_date.isoformat(), emp.name, f"{total_hours:.2f}", f"{reg:.2f}", f"{ot:.2f}"])

    filename = f"payroll_{loc.name}_{week_start_date.isoformat()}.csv"
    return Response(
        out.getvalue().encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
    
@app.route('/api/employee_status/<int:employee_id>')
def api_employee_status(employee_id: int):
    emp = Employee.query.get(employee_id)
    if not emp or getattr(emp, "active", True) is False:
        return jsonify({"ok": False, "status": "INACTIVE"}), 404

    last = (Punch.query
            .filter_by(employee_id=employee_id)
            .order_by(Punch.timestamp.desc())
            .first())

    if not last:
        return jsonify({"ok": True, "status": "OUT", "last_type": None, "last_time": None})

    status = "IN" if last.type == "IN" else "OUT"
    return jsonify({
        "ok": True,
        "status": status,
        "last_type": last.type,
        "last_time_utc": last.timestamp.isoformat()
    })


@app.route('/weekly_report')
@supervisor_required
def weekly_report():
    """
    Detailed weekly report showing every IN/OUT (rounded to 15min) for each employee,
    with a dropdown to select any week (Monday) in the past 3 months, and a location selector.
    Now also rounds the weekly total hours to the nearest 15 minutes using the 7/8 rule.
    Query params:
      - loc:        integer Location.id
      - week_start: ISO date string (YYYY-MM-DD) for the Monday to display.
                    If missing or invalid, defaults to this week’s Monday.
    """

    # 1) Get all locations for the dropdown
    locations = Location.query.order_by(Location.name).all()

    # 2) Determine which location was selected (default = first)
    try:
        loc_id = int(request.args.get('loc', locations[0].id))
    except (ValueError, IndexError):
        flash('Invalid location selected.', 'warning')
        loc_id = locations[0].id

    loc = Location.query.get(loc_id)
    if not loc:
        flash('Invalid location', 'danger')
        return redirect(url_for('weekly_report', loc=locations[0].id))


    # ✅ Supervisors can only view their own location
    require_user_location_scope(loc_id)

    # 3) Compute local timezone and “today” in that tz
    tz = ZoneInfo(TIMEZONES[loc.name])
    today_local = datetime.now(tz)

    # 4) Build list of all Mondays in the past 90 days
    days_since_monday = today_local.weekday()  # Monday=0
    this_monday = (today_local - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).date()

    three_months_ago = today_local.date() - timedelta(days=90)
    mondays = []
    m = this_monday
    while m >= three_months_ago:
        mondays.append(m)
        m -= timedelta(days=7)
    mondays.sort(reverse=True)  # most recent first

    # 5) Parse week_start from query (or default to this_monday)
    week_start_str = request.args.get('week_start')
    if week_start_str:
        try:
            candidate = datetime.fromisoformat(week_start_str).date()
            if candidate not in mondays:
                raise ValueError()
            week_start_date = candidate
        except (ValueError, TypeError):
            flash('Invalid week selected. Showing current week.', 'warning')
            week_start_date = this_monday
    else:
        week_start_date = this_monday

    # 6) Convert week_start_date → local datetime → UTC for querying
    week_start_local = datetime.combine(week_start_date, datetime.min.time(), tzinfo=tz)
    week_end_local   = week_start_local + timedelta(days=7)
    week_start_utc   = week_start_local.astimezone(ZoneInfo('UTC'))
    week_end_utc     = week_end_local.astimezone(ZoneInfo('UTC'))

    # 7) Fetch punches for this location in that UTC window
    punches = (
        Punch.query
             .join(Employee)
             .filter(
                 Employee.location_id == loc_id,
                 Punch.timestamp >= week_start_utc,
                 Punch.timestamp <  week_end_utc
             )
             .order_by(Punch.employee_id, Punch.timestamp)
             .all()
    )

    # 8) Helper: round a datetime to nearest 15 minutes (7/8 rule)
    def round_to_15(dt_local):
        minute = dt_local.minute
        remainder = minute % 15
        if remainder < 8:
            new_minute = minute - remainder
        else:
            new_minute = minute + (15 - remainder)
        if new_minute == 60:
            dt_local = dt_local.replace(
                hour=(dt_local.hour + 1) % 24,
                minute=0, second=0, microsecond=0
            )
        else:
            dt_local = dt_local.replace(
                minute=new_minute, second=0, microsecond=0
            )
        return dt_local

    # 9) Organize punches by employee → local date → list of (type, rounded dt)
    by_emp = defaultdict(lambda: defaultdict(list))
    for p in punches:
        # convert UTC→local, then round
        local_dt = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        rounded_dt = round_to_15(local_dt)
        local_date = rounded_dt.date()
        by_emp[p.employee_id][local_date].append((p.type, rounded_dt))

    # 10) Fetch all employees at this location
    # ✅ Hide terminated employees unless they have punches in the selected week
    emp_ids_with_punches = {p.employee_id for p in punches}
    
    employees = (
        Employee.query
        .filter(Employee.location_id == loc_id)
        .filter(
            (Employee.active.is_(True)) |
            (Employee.id.in_(emp_ids_with_punches))
        )
        .order_by(Employee.active.desc(), Employee.name.asc())
        .all()
    )

    # 11) Precompute the seven Monday→Sunday dates
    dates = [week_start_date + timedelta(days=i) for i in range(7)]

    # 12) Helper: round total seconds to nearest 900 seconds (15 minutes)
    def round_secs_to_15(total_secs):
        remainder = total_secs % 900  # 900 = 15 * 60
        if remainder < 450:
            return total_secs - remainder
        else:
            return total_secs + (900 - remainder)

    # 13) Build report_data, including a `week_total_hrs` rounded to nearest 15 min
    report_data = []
    for emp in employees:
        row = {
            'employee_name': emp.name,
            'daily_events': {},      # { date: [ ('IN', dt), ('OUT', dt), … ] }
            'week_total_hrs': 0.0    # will fill below
        }
        week_seconds = 0

        for d in dates:
            events = by_emp[emp.id].get(d, [])
            events.sort(key=lambda x: x[1])  # chronological
            row['daily_events'][d] = events

            # Sum that day’s worked seconds by pairing IN→OUT
            daily_seconds = 0
            last_in = None
            for ev_type, ev_dt in events:
                if ev_type == 'IN':
                    last_in = ev_dt
                elif ev_type == 'OUT' and last_in:
                    delta = (ev_dt - last_in).total_seconds()
                    if delta > 0:
                        daily_seconds += delta
                    last_in = None
            week_seconds += daily_seconds

        # Now round the total week_seconds to nearest 15 minutes
        rounded_week_secs = round_secs_to_15(week_seconds)
        # Convert to hours with two decimals
        row['week_total_hrs'] = round(rounded_week_secs / 3600, 2)

        report_data.append(row)

    # 14) Render the template with all context
    return render_template(
        'weekly_report.html',
        locations=locations,
        loc=loc,
        mondays=mondays,
        selected_monday=week_start_date,
        dates=dates,
        report_data=report_data
    )

@app.route('/manage_employees', methods=['GET','POST'])
@admin_required
def manage_employees():

    if request.method == 'POST':
        if 'add' in request.form:
            name = request.form['name'].strip()
            lid  = int(request.form['loc'])
            db.session.add(Employee(name=name, location_id=lid))
            flash(f'Employee "{name}" added.', 'success')

        elif 'remove' in request.form:
            eid = int(request.form['eid'])
            emp = Employee.query.get(eid)
            if emp:
                emp.active = False
                emp.terminated_at = datetime.utcnow()
                flash(f'Removed "{emp.name}" from active roster (history preserved).', 'success')
            else:
                flash('Employee not found.', 'warning')

        elif 'reactivate' in request.form:
            eid = int(request.form['eid'])
            emp = Employee.query.get(eid)
            if emp:
                emp.active = True
                emp.terminated_at = None
                flash(f'Reactivated "{emp.name}".', 'success')
            else:
                flash('Employee not found.', 'warning')

        db.session.commit()
        return redirect(url_for('manage_employees'))

    # GET: safe ordering even if active isn't present yet
    active_col = getattr(Employee, "active", None)
    if active_col is not None:
        emps = Employee.query.order_by(active_col.desc(), Employee.name.asc()).all()
    else:
        emps = Employee.query.order_by(Employee.name.asc()).all()

    return render_template(
        'manage_employees.html',
        locations=Location.query.all(),
        emps=emps
    )

@app.route('/login', methods=['GET','POST'])
def login():
    """
    ✅ Single login page for ALL roles.
    - employee -> clock
    - supervisor/admin -> weekly report (scoped to their location if set)
    """
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        u = User.query.filter_by(username=username).first()

        if not u or not u.check_password(password):
            flash('Invalid credentials', 'danger')
            return redirect(url_for('login'))

        if getattr(u, "active", True) is False:
            flash("Account disabled. Contact an admin.", "danger")
            return redirect(url_for('login'))

        login_user(u)

        # ✅ Redirect based on role
        role = (getattr(u, "role", "employee") or "employee").lower()

        if role == "admin":
            return redirect(url_for("admin_dashboard"))
        
        if role == "supervisor":
            loc_id = getattr(u, "location_id", None) or 1
            return redirect(url_for("weekly_report", loc=int(loc_id)))

        return redirect(url_for('index'))

    return render_template('login.html')


@app.route("/admin")
@admin_required
def admin_root():
    return redirect(url_for("admin_dashboard"))
    
@app.route('/manager_login', methods=['GET','POST'])
def manager_login():
    # ✅ Backward compatibility: old bookmark -> new unified login
    return redirect(url_for('login'))

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    # Enterprise hub metrics
    locations = Location.query.order_by(Location.name.asc()).all()

    stats = {
        "users_total": User.query.count(),
        "employees_total": Employee.query.count(),
        "employees_active": Employee.query.filter_by(active=True).count(),
        "punches_total": Punch.query.count(),
        "audit_total": PunchAudit.query.count(),
    }

    # Latest audit entries (lightweight)
    recent_audit = (PunchAudit.query
                    .order_by(PunchAudit.created_at.desc())
                    .limit(12)
                    .all())

    return render_template(
        "admin/dashboard.html",
        locations=locations,
        stats=stats,
        recent_audit=recent_audit,
    )

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
