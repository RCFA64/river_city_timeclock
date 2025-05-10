from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from models import db, Location, Employee, Punch, User
from utils import compute_shifts
import math
import os
from dotenv import load_dotenv

# Per-location timezones
TIMEZONES = {
    'Sacramento':   'America/Los_Angeles',
    'Dallas':       'America/Chicago',
    'Houston':      'America/Chicago',
    'Indianapolis': 'America/New_York'
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ['SECRET_KEY'],
    SQLALCHEMY_DATABASE_URI=os.environ['DATABASE_URL'],
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)
load_dotenv()

# User loader for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Bootstrap schema and seed locations
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
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=5*30)
        Punch.query.filter(Punch.timestamp < cutoff).delete()
        db.session.commit()

sched = BackgroundScheduler()
sched.add_job(purge_old, trigger='cron', hour=0, minute=0)
sched.start()

# Haversine distance (meters)
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

@app.route('/')
def index():
    # Locations
    locs = Location.query.all()
    sel = int(request.args.get('loc', locs[0].id if locs else 1))
    location = Location.query.get(sel)
    tz = ZoneInfo(TIMEZONES[location.name])

    # Current date in local tz
    current_date = datetime.now(tz).strftime('%A, %B %d, %Y')

    # Calculate today's UTC window
    now_local      = datetime.now(tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_local = midnight_local + timedelta(days=1)
    start_utc = midnight_local.astimezone(ZoneInfo('UTC'))
    end_utc   = tomorrow_local.astimezone(ZoneInfo('UTC'))

    # Selected employee (optional)
    sel_emp = request.args.get('emp', None)
    emp_id = int(sel_emp) if sel_emp else None

    # Build base query for punches
    query = (Punch.query.join(Employee)
             .filter(
                 Employee.location_id == sel,
                 Punch.timestamp >= start_utc,
                 Punch.timestamp <  end_utc
             ))
    if emp_id:
        query = query.filter(Punch.employee_id == emp_id)

    raw = (query
           .order_by(Punch.timestamp.desc())
           .limit(20)
           .all())

    # Format feed entries
    feed = []
    for p in raw:
        local_ts = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        feed.append({
            'time_str': local_ts.strftime('%I:%M:%S %p'),
            'employee': p.employee.name,
            'type':     p.type
        })

    # Employees for this location
    employees = Employee.query.filter_by(location_id=sel).all()

    return render_template(
        'index.html',
        locations=locs,
        sel=sel,
        sel_emp=sel_emp,
        emps=employees,
        feed=feed,
        current_date=current_date
    )

@app.route('/punch', methods=['POST'])
def punch():
    lat_str = request.form.get('geo_lat', '')
    lng_str = request.form.get('geo_lng', '')
    try:
        user_lat = float(lat_str)
        user_lng = float(lng_str)
    except ValueError:
        flash('Unable to get your location. Please allow location access and try again.', 'danger')
        return redirect(url_for('index', loc=request.form.get('loc')))

    loc_id = int(request.form['loc'])
    loc    = Location.query.get(loc_id)

    # Geo-fence: 200m radius
    if haversine(user_lat, user_lng, loc.lat, loc.lng) > 200:
        flash('You must be on-site to punch in/out.', 'danger')
        return redirect(url_for('index', loc=loc_id, emp=eid))

    # Create punch
    p = Punch(employee_id=eid, type=request.form['type'], timestamp=datetime.utcnow())
    db.session.add(p)
    db.session.commit()

    return redirect(url_for('index', loc=loc_id, emp=eid))

@app.route('/report')
@login_required
def report():
    loc = int(request.args.get('loc', 1))
    tz  = ZoneInfo(TIMEZONES[Location.query.get(loc).name])

    cutoff = datetime.utcnow() - timedelta(days=5*30)
    punches = (Punch.query.join(Employee)
               .filter(
                   Employee.location_id == loc,
                   Punch.timestamp >= cutoff
               )
               .order_by(Employee.id, Punch.timestamp)
               .all())

    # Group by employee & week
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
            'employee':   emp.name,
            'week_start': week_start,
            'total':      stats['total']
        })

    reports.sort(key=lambda r: (r['week_start'], r['employee']))
    return render_template(
        'report.html',
        reports=reports,
        loc=loc,
        locations=Location.query.all()
    )

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
        elif 'remove' in request.form:
            eid = int(request.form['eid'])
            emp = Employee.query.get(eid)
            db.session.delete(emp)
        db.session.commit()
        return redirect(url_for('manage_employees'))

    return render_template(
        'manage_employees.html',
        locations=Location.query.all(),
        emps=Employee.query.all()
    )

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
