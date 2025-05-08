from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user, UserMixin
)
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Location, Employee, Punch, User
from utils import compute_shifts
import math
import os

# Per-location timezones
TIMEZONES = {
    'Sacramento':   'America/Los_Angeles',
    'Dallas':       'America/Chicago',
    'Houston':      'America/Chicago',
    'Indianapolis': 'America/New_York'
}

app = Flask(__name__)
app.config.update(
  SECRET_KEY=os.environ['zYHKLNBYy6RFLlXylv2RzCqUK3CvnCXQ'],
  SQLALCHEMY_DATABASE_URI=os.environ['postgresql://timeclock_db_x81m_user:rLVZoYLFUuje8PWHQ4SnAW48nIOtUA9E@dpg-d0egnmh5pdvs73amotfg-a/timeclock_db_x81m'],
  SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# *** Add this user_loader callback ***
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Bootstrap the schema + seed your 4 locations
with app.app_context():
    db.create_all()
    coords = [
        ('Sacramento',   38.5816, -121.4944),
        ('Dallas',       32.7767,  -96.7970),
        ('Houston',      29.7604,  -95.3698),
        ('Indianapolis', 39.7684,  -86.1581),
    ]
    for name, lat, lng in coords:
        if not Location.query.filter_by(name=name).first():
            db.session.add(Location(name=name, lat=lat, lng=lng))
    db.session.commit()


# Purge 5-month-old punches nightly
def purge_old():
    cutoff = datetime.utcnow() - timedelta(days=5*30)
    Punch.query.filter(Punch.timestamp < cutoff).delete()
    db.session.commit()

sched = BackgroundScheduler()
sched.add_job(purge_old, trigger='cron', hour=0, minute=0)
sched.start()


# Haversine distance in meters
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


@app.route('/')
def index():
    locs = Location.query.all()
    sel = int(request.args.get('loc', locs[0].id if locs else 1))
    location = Location.query.get(sel)
    tz = ZoneInfo(TIMEZONES[location.name])

    current_date = datetime.now(tz).strftime('%A, %B %d, %Y')

    now_local      = datetime.now(tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_local = midnight_local + timedelta(days=1)
    start_utc = midnight_local.astimezone(ZoneInfo('UTC'))
    end_utc   = tomorrow_local.astimezone(ZoneInfo('UTC'))

    raw = (Punch.query.join(Employee)
           .filter(Employee.location_id==sel,
                   Punch.timestamp>=start_utc,
                   Punch.timestamp< end_utc)
           .order_by(Punch.timestamp.desc())
           .limit(20)
           .all())

    feed = []
    for p in raw:
        local_ts = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        feed.append({
            'time_str': local_ts.strftime('%I:%M:%S %p'),
            'employee': p.employee.name,
            'type':     p.type
        })

    employees = Employee.query.filter_by(location_id=sel).all()
    return render_template('index.html',
                           locations=locs,
                           sel=sel,
                           emps=employees,
                           feed=feed,
                           current_date=current_date)


@app.route('/punch', methods=['POST'])
def punch():
    loc_id   = int(request.form['loc'])
    loc      = Location.query.get(loc_id)
    user_lat = float(request.form.get('geo_lat', 0))
    user_lng = float(request.form.get('geo_lng', 0))

    if haversine(user_lat, user_lng, loc.lat, loc.lng) > 200:
        flash('You must be on-site to punch in/out.', 'danger')
        return redirect(url_for('index', loc=loc_id))

    eid = int(request.form['employee_id'])
    typ = request.form['type']
    p   = Punch(employee_id=eid, type=typ, timestamp=datetime.utcnow())
    db.session.add(p)
    db.session.commit()

    return redirect(url_for('index', loc=loc_id))


@app.route('/report')
@login_required
def report():
    loc = int(request.args.get('loc', 1))
    tz  = ZoneInfo(TIMEZONES[Location.query.get(loc).name])

    cutoff = datetime.utcnow() - timedelta(days=5*30)
    punches = (Punch.query.join(Employee)
               .filter(Employee.location_id==loc,
                       Punch.timestamp>=cutoff)
               .order_by(Employee.id, Punch.timestamp)
               .all())

    from collections import defaultdict
    weekly = defaultdict(list)
    for p in punches:
        local_dt = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        monday   = local_dt.date() - timedelta(days=local_dt.weekday())
        weekly[(p.employee_id, monday)].append(local_dt)

    reports = []
    for (eid, week_start), times in weekly.items():
        times.sort()
        stats = compute_shifts(times)
        emp   = Employee.query.get(eid)
        reports.append({
            'employee':    emp.name,
            'week_start':  week_start,
            'total':       stats['total']
        })

    reports.sort(key=lambda r: (r['week_start'], r['employee']))
    return render_template('report.html',
                           reports=reports,
                           loc=loc,
                           locations=Location.query.all())


@app.route('/manage_employees', methods=['GET','POST'])
@login_required
def manage_employees():
    if not current_user.is_manager:
        flash('Not authorized', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        if 'add' in request.form:
            name = request.form['name']
            lid  = int(request.form['loc'])
            db.session.add(Employee(name=name, location_id=lid))
            db.session.commit()
        elif 'remove' in request.form:
            eid = int(request.form['eid'])
            emp = Employee.query.get(eid)
            db.session.delete(emp)
            db.session.commit()
        return redirect(url_for('manage_employees'))

    return render_template('manage_employees.html',
                           locations=Location.query.all(),
                           emps=Employee.query.all())


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and u.check_password(request.form['password']):
            login_user(u)
            return redirect(url_for('index'))

        flash('Invalid credentials', 'danger')
        return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(debug=True)
