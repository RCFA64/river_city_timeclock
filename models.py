from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

db = SQLAlchemy()

class Location(db.Model):
    __tablename__ = 'locations'
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(50), unique=True, nullable=False)
    lat      = db.Column(db.Float, nullable=False)   # e.g. 34.0522
    lng      = db.Column(db.Float, nullable=False)   # e.g. -118.2437
    employees = db.relationship('Employee', back_populates='location')

class Employee(db.Model):
    __tablename__ = 'employees'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False)
    location    = db.relationship('Location', back_populates='employees')
    punches     = db.relationship('Punch', back_populates='employee')

class Punch(db.Model):
    __tablename__ = 'punches'
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    type        = db.Column(db.Enum('IN', 'OUT', name='punch_type'), nullable=False)
    employee    = db.relationship('Employee', back_populates='punches')

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_manager    = db.Column(db.Boolean, default=False)

    def set_password(self, pwd):
        self.password_hash = generate_password_hash(pwd)

    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)
