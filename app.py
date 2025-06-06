from flask import Flask, render_template, request, redirect, url_for, flash, current_app
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

TIMEZONES = {
    'Sacramento':   'America/Los_Angeles',
    'Dallas':       'America/Chicago',
    'Houston':      'America/Chicago',
    'Indianapolis': 'America/New_York'
}

app = Flask(__name__)

load_dotenv()

app.config.update(
    SECRET_KEY=os.environ['SECRET_KEY'],
    SQLALCHEMY_DATABASE_URI=os.environ['DATABASE_URL'],
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

# Bootstrap schema and seed locations
with app.app_context():
    db.create_all()

    coords = [
        # Sacramento (your shop)
        ('Sacramento',   38.535168, -121.3661184),
        # Dallas (your shop)
        ('Dallas',       32.5372008,  -96.7493993),
        # Houston (your shop)
        ('Houston',      29.835264,  -95.5383808),
        # Indianapolis (your shop)
        ('Indianapolis', 39.6836058,  -86.1927711),
    ]
    for name, lat, lng in coords:
        loc = Location.query.filter_by(name=name).first()
        if loc:
            # update existing
            loc.lat, loc.lng = lat, lng
        else:
            # create new
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
    emps = Employee.query.filter_by(location_id=sel).all()

    return render_template('index.html',
                           locations=locs,
                           sel=sel,
                           emps=emps,
                           feed=feed,
                           current_date=current_date,
                           emp=emp)

@app.route('/punch', methods=['POST'])
def punch():
    loc_id = request.form.get('loc', type=int)
    emp_val = request.form.get('employee_id')
    if not emp_val:
        flash('Please select an employee before punching.', 'warning')
        return redirect(url_for('index', loc=loc_id))

    eid = int(emp_val)

    # Always non-empty now!
    punch_type = request.form.get('type', 'IN')
    p = Punch(employee_id=eid, type=punch_type, timestamp=datetime.utcnow())
    db.session.add(p)
    db.session.commit()

    return redirect(url_for('index', loc=loc_id, emp=eid))

@app.route('/report')
@login_required
def report():
    loc = int(request.args.get('loc', 1))
    tz  = ZoneInfo(TIMEZONES[Location.query.get(loc).name])

    # Grab everything in the last two months (or however far back you want)
    cutoff = datetime.utcnow() - timedelta(days=60)
    punches = (Punch.query.join(Employee)
               .filter(Employee.location_id==loc,
                       Punch.timestamp>=cutoff)
               .order_by(Punch.timestamp)
               .all())

    # First, bucket by week
    from collections import defaultdict, OrderedDict
    weekly = defaultdict(list)
    for p in punches:
        local_dt = p.timestamp.replace(tzinfo=ZoneInfo('UTC')).astimezone(tz)
        # Determine the week span
        monday = local_dt.date() - timedelta(days=local_dt.weekday())
        sunday = monday + timedelta(days=6)
        week_key = (monday, sunday)
        weekly[week_key].append((p.employee_id, local_dt))

    # Anything older than 4 weeks?  Move into months
    four_weeks_ago = (datetime.now(tz).date() 
                      - timedelta(weeks=4))
    monthly = defaultdict(list)
    for (monday, sunday), entries in list(weekly.items()):
        if sunday < four_weeks_ago:
            # pull out and re‐bucket by month
            for eid, dt in entries:
                month_key = dt.strftime('%Y-%m')
                monthly[month_key].append((eid, dt))
            del weekly[(monday, sunday)]

    # Now build display structures
    def summarize(entries):
        by_emp = defaultdict(list)
        for eid, dt in entries:
            by_emp[eid].append(dt)
        rows = []
        for eid, times in by_emp.items():
            times.sort()
            stats = compute_shifts(times)
            emp   = Employee.query.get(eid)
            rows.append({
                'employee': emp.name,
                'total':    stats['total']
            })
        return rows

    report_weeks = OrderedDict()
    for (monday, sunday), entries in sorted(weekly.items()):
        label = f"{monday:%-m/%-d/%y}–{sunday:%-m/%-d/%y}"
        report_weeks[label] = summarize(entries)

    report_months = OrderedDict()
    for month_key, entries in sorted(monthly.items()):
        # month_key = '2025-04', make it pretty:
        dt = datetime.strptime(month_key, '%Y-%m')
        label = dt.strftime('%B %Y')
        report_months[label] = summarize(entries)

    return render_template('report.html',
                           weeks=report_weeks,
                           months=report_months,
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
    """
    EMPLOYEE login page. Only User.is_manager == False may log in here.
    After successful login, redirect to the clock page (index).
    """
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and u.check_password(request.form['password']) and not u.is_manager:
            login_user(u)
            return redirect(url_for('index'))
        flash('Invalid employee credentials', 'danger')
        return redirect(url_for('login'))

    # Render the same login.html template (no manager flag)
    return render_template('login.html')


@app.route('/manager_login', methods=['GET','POST'])
def manager_login():
    """
    MANAGER login page. Only User.is_manager == True may log in here.
    After successful login, redirect to the report page.
    """
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and u.check_password(request.form['password']) and u.is_manager:
            login_user(u)
            # Redirect manager to /report (default loc=1)
            return redirect(url_for('report', loc=1))
        flash('Invalid manager credentials', 'danger')
        return redirect(url_for('manager_login'))

    # Render login.html but pass a flag so the template knows it's for managers
    return render_template('login.html', manager=True)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
