"""The single company-settings screen (spec §2). System-admin only.

Every change goes through a confirmation step showing name / old value / new
value / effective-from and the fixed spec sentence, then is audited.
"""
from datetime import datetime

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from app.admin.models import SettingDefinition
from app.core import settings as settings_service
from app.core.permissions import requires
from app.extensions import db

bp = Blueprint("admin", __name__)

# The fixed confirmation sentence mandated by spec §2 rule 2.
CONFIRM_SENTENCE = "هذا التغيير يسري على العمليات الجديدة فقط، والعمليات السابقة تبقى كما سُجلت."

SECTIONS = [
    ("company", "الشركة والدول"),
    ("branches", "الفروع"),
    ("treasury", "الخزائن والحسابات البنكية"),
    ("finance", "المالية"),
    ("inventory", "المخزون"),
    ("sales", "المبيعات ونقطة البيع"),
    ("users", "المستخدمون والصلاحيات"),
]


@bp.before_request
@login_required
def guard():
    # Settings screen is reachable only by the system-admin permission (§2).
    if not current_user.has_permission("settings.manage"):
        abort(403)


@bp.route("/")
def index():
    counts = {}
    for key, _ in SECTIONS:
        counts[key] = db.session.scalar(
            db.select(db.func.count(SettingDefinition.id)).filter_by(section=key)
        )
    return render_template("admin/index.html", sections=SECTIONS, counts=counts)


@bp.route("/section/<section>")
def section(section):
    defs = db.session.scalars(
        db.select(SettingDefinition).filter_by(section=section).order_by(
            SettingDefinition.id
        )
    ).all()
    title = dict(SECTIONS).get(section, section)
    rows = [
        {"def": d, "current": settings_service.get(d.key), "history": d.values}
        for d in defs
    ]
    return render_template(
        "admin/section.html", section=section, title=title, rows=rows
    )


@bp.route("/change/<key>", methods=["GET", "POST"])
@requires("settings.manage")
def change(key):
    definition = db.session.scalar(
        db.select(SettingDefinition).filter_by(key=key)
    )
    if definition is None:
        abort(404)

    old_value = settings_service.get(key)

    if request.method == "GET":
        # Step 1: show the change form (HTMX modal).
        return render_template(
            "admin/_change_form.html",
            definition=definition,
            old_value=old_value,
            confirm_sentence=CONFIRM_SENTENCE,
            confirmed=False,
        )

    new_value = request.form.get("value", "").strip()
    confirmed = request.form.get("confirmed") == "1"

    if definition.is_locked:
        return render_template(
            "admin/_change_form.html",
            definition=definition,
            old_value=old_value,
            error="هذا الإعداد مقفول بعد أول حركة ولا يمكن تعديله.",
            confirm_sentence=CONFIRM_SENTENCE,
            confirmed=False,
        )

    if not confirmed:
        # Step 2: show the confirmation with old/new/effective-from + sentence.
        return render_template(
            "admin/_change_form.html",
            definition=definition,
            old_value=old_value,
            new_value=new_value,
            effective_from=datetime.utcnow(),
            confirm_sentence=CONFIRM_SENTENCE,
            confirmed=True,
        )

    # Step 3: persist + audit.
    settings_service.set(key, new_value, user_id=current_user.id)
    db.session.commit()

    # Return the refreshed row as an out-of-band swap; the modal target
    # receives it and, being OOB, the modal itself clears.
    return render_template(
        "admin/_setting_row.html",
        row={
            "def": definition,
            "current": settings_service.get(key),
            "history": definition.values,
        },
        saved=True,
        oob=True,
    )
