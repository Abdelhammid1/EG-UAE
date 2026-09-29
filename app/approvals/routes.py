"""Approvals inbox screen."""
from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

from app.approvals import services
from app.core.permissions import requires

bp = Blueprint("approvals", __name__)


@bp.before_request
@login_required
def _guard():
    pass


@bp.route("/")
@requires("accounting.journal.post")
def index():
    return render_template("approvals/index.html", s=services.summary())
