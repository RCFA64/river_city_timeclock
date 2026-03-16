# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

River City Timeclock — a Flask web app for multi-location employee time tracking at River City Furniture Auction (RCFA). Four locations: Sacramento, Dallas, Houston, Indianapolis, each with assigned timezones.

## Tech Stack

- **Backend**: Flask 3.1, Flask-Login, Flask-SQLAlchemy, APScheduler
- **Database**: PostgreSQL (psycopg2-binary), all timestamps stored in UTC
- **Frontend**: Bootstrap 5 (CDN), vanilla JS, Jinja2 templates
- **Deployment**: Gunicorn on Render.com (see Procfile)

## Commands

```bash
# Setup
pip install -r requirements.txt

# Run dev server
python app.py

# Production
gunicorn app:app
```

There are no tests or linting configured.

## Architecture

### Single-file Flask app (`app.py`)

The entire routing and business logic lives in `app.py` (~1100 lines). Key patterns:

- **Role-based access**: Three roles — employee, supervisor, admin. Decorators `@admin_required`, `@supervisor_required`, `@manager_required` enforce access. Supervisors are location-scoped via `require_user_location_scope()`.
- **Schema migration**: No Alembic. `ensure_schema()` runs on startup, inspects the DB, and applies ALTER TABLE statements for missing columns. New schema changes go here.
- **Background jobs**: APScheduler purges punches older than 5 months nightly.
- **Bootstrap admin**: First admin user created from `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` env vars.

### Models (`models.py`)

- **Location** — office sites with lat/lng
- **Employee** — workers with location FK, soft-delete via `active` + `terminated_at`
- **Punch** — IN/OUT records with UTC timestamps
- **PunchAudit** — immutable audit trail for all punch modifications (EDIT/DELETE/CREATE)
- **User** — system users with role and optional location scope

### Time handling (`utils.py`)

- `round_time()` — 5-minute rounding for clock punches
- Shift computation with 8-hour regular + overtime calculation
- Payroll CSV export uses 15-minute rounding
- Timezone mapping is hardcoded in `app.py` `TIMEZONES` dict (location name → tz string)

### Route groups

| Prefix | Purpose |
|--------|---------|
| `/` | Employee clock in/out interface |
| `/kiosk` | Public clock mode (optional `KIOSK_KEY` protection) |
| `/login`, `/logout` | Authentication |
| `/weekly_report` | Supervisor/admin weekly hours report |
| `/admin/*` | User mgmt, punch editing, audit log, employee mgmt, dashboard |
| `/api/employee_status/<id>` | JSON status endpoint |

### Templates

`templates/base.html` is the shared layout. Admin templates are under `templates/admin/`. The UI uses a gold (#D4AF37) and charcoal (#2B2B2B) color scheme.

## Environment Variables

- `SECRET_KEY` — Flask session secret
- `DATABASE_URL` — PostgreSQL connection string
- `KIOSK_KEY` — optional API key for kiosk endpoint
- `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` — initial admin credentials
