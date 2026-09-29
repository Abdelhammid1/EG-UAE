"""Role-aware home. Each role lands on the surface it actually uses (UI law:
the home screen is the role)."""
from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.auth.models import User
from app.core.audit import AuditLog
from app.core.models import Branch
from app.extensions import db

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def home():
    # Route each role to its natural landing surface.
    if current_user.has_role("cashier") and not current_user.has_role("accountant"):
        return render_template("main/cashier_home.html")

    # Owner / accountant / sysadmin: a work-oriented dashboard.
    stats = {
        "branches": db.session.scalar(db.select(db.func.count(Branch.id))),
        "users": db.session.scalar(db.select(db.func.count(User.id))),
        "audit_events": db.session.scalar(db.select(db.func.count(AuditLog.id))),
    }
    recent_audit = db.session.scalars(
        db.select(AuditLog).order_by(AuditLog.at.desc()).limit(8)
    ).all()
    return render_template("main/dashboard.html", stats=stats, recent_audit=recent_audit)


@bp.route("/audit")
@login_required
def audit_log():
    from app.core.permissions import can

    if not can("audit.view"):
        from flask import abort
        abort(403)

    page = 1
    entries = db.session.scalars(
        db.select(AuditLog).order_by(AuditLog.at.desc()).limit(100)
    ).all()
    return render_template("main/audit_log.html", entries=entries)
