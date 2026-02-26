from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

db = SQLAlchemy()

class Location(db.Model):
    __tablename__ = 'locations'
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(50), unique=True, nullable=False)
    lat      = db.Column(db.Float, nullable=False)
    lng      = db.Column(db.Float, nullable=False)
    employees = db.relationship('Employee', back_populates='location')

class Employee(db.Model):
    __tablename__ = 'employees'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False)

    # ✅ Hide terminated employees everywhere, preserve history
    active        = db.Column(db.Boolean, default=True, nullable=False)
    terminated_at = db.Column(db.DateTime, nullable=True)

    location    = db.relationship('Location', back_populates='employees')
    punches     = db.relationship(
        'Punch',
        back_populates='employee',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

class Punch(db.Model):
    __tablename__ = 'punches'
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    type        = db.Column(db.Enum('IN', 'OUT', name='punch_type'), nullable=False)
    employee    = db.relationship('Employee', back_populates='punches')

class PunchAudit(db.Model):
    """Immutable audit log for punch modifications."""
    __tablename__ = 'punch_audits'
    id = db.Column(db.Integer, primary_key=True)

    punch_id = db.Column(db.Integer, db.ForeignKey('punches.id', ondelete='SET NULL'), nullable=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='SET NULL'), nullable=True)

    changed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    action = db.Column(db.String(20), nullable=False)  # EDIT / DELETE / CREATE

    old_type = db.Column(db.String(8), nullable=True)
    new_type = db.Column(db.String(8), nullable=True)
    old_timestamp = db.Column(db.DateTime, nullable=True)
    new_timestamp = db.Column(db.DateTime, nullable=True)

    note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    # ✅ Role-based access
    # employee = can clock only
    # supervisor = can view reports + edit punches
    # admin = can manage users + employees + payroll
    role = db.Column(db.String(20), default="employee", nullable=False)
    
    # ✅ Allow disabling accounts without deleting history
    active = db.Column(db.Boolean, default=True, nullable=False)

    # ✅ Scope supervisors to one location (Admins can be global / None)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True)
    location = db.relationship('Location')

    def set_password(self, pwd):
        self.password_hash = generate_password_hash(pwd)

    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)

    @property
    def is_admin(self) -> bool:
        return (self.role or "").lower() == "admin"
    
    @property
    def is_supervisor(self) -> bool:
        return (self.role or "").lower() in ("supervisor", "admin")    