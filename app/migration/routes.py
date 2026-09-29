"""Data-migration screens (spec §M13)."""
from __future__ import annotations

import json

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required

from app.core.permissions import requires
from app.extensions import db
from app.migration import services
from app.migration.models import ImportBatch

bp = Blueprint("migration", __name__)


@bp.before_request
@login_required
def _guard():
    pass


@bp.route("/")
@requires("accounting.journal.create")
def index():
    batches = db.session.scalars(
        db.select(ImportBatch).order_by(ImportBatch.id.desc()).limit(20)).all()
    return render_template("migration/index.html", kinds=services.KINDS,
                           batches=batches)


@bp.route("/template/<kind>")
@requires("accounting.journal.create")
def template(kind):
    if kind not in services.KINDS:
        abort(404)
    buf = services.generate_template(kind)
    return send_file(buf, as_attachment=True,
                     download_name=f"template-{kind}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/upload", methods=["POST"])
@requires("accounting.journal.create")
def upload():
    kind = request.form.get("kind")
    if kind not in services.KINDS:
        abort(400)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("اختر ملفًا.", "error")
        return redirect(url_for("migration.index"))
    try:
        headers, raw = services.read_sheet(file)
    except Exception:
        flash("تعذّر قراءة الملف. تأكد أنه بصيغة Excel (.xlsx).", "error")
        return redirect(url_for("migration.index"))
    if not raw:
        flash("الملف فارغ — لا توجد بيانات لاستيرادها.", "error")
        return redirect(url_for("migration.index"))
    # stash the raw sheet on the batch and let the user confirm the column mapping
    batch = ImportBatch(
        kind=kind, filename=file.filename, status="mapping",
        rows_json=json.dumps({"raw": raw, "headers": headers},
                             ensure_ascii=False, default=str),
        row_count=len(raw), error_count=0, created_by_id=current_user.id)
    db.session.add(batch)
    db.session.commit()
    return redirect(url_for("migration.map_columns", bid=batch.id))


@bp.route("/<int:bid>/map", methods=["GET", "POST"])
@requires("accounting.journal.create")
def map_columns(bid):
    """Map the uploaded file's columns to our fields, then validate (MIG)."""
    batch = db.get_or_404(ImportBatch, bid)
    stash = json.loads(batch.rows_json) if batch.rows_json else {}
    headers = stash.get("headers", [])
    raw = stash.get("raw", [])
    spec = services.KINDS[batch.kind]
    if batch.status != "mapping":
        return redirect(url_for("migration.view", bid=bid))

    if request.method == "POST":
        mapping = {}
        for col in spec["columns"]:
            v = request.form.get(f"map_{col}")
            mapping[col] = int(v) if (v not in (None, "", "-1") and v.isdigit()) else None
        rows = services.parse_with_mapping(batch.kind, raw, mapping)
        rows, error_count = services.validate(batch.kind, rows)
        batch.status = "validated"
        batch.rows_json = json.dumps(rows, ensure_ascii=False, default=str)
        batch.row_count = len(rows)
        batch.error_count = error_count
        db.session.commit()
        return redirect(url_for("migration.view", bid=bid))

    suggested = services.suggest_mapping(batch.kind, headers)
    # a small preview of the first rows for context
    preview = raw[:5]
    return render_template("migration/map.html", batch=batch, spec=spec,
                           headers=headers, suggested=suggested, preview=preview)


@bp.route("/<int:bid>")
@requires("accounting.journal.create")
def view(bid):
    batch = db.get_or_404(ImportBatch, bid)
    if batch.status == "mapping":
        return redirect(url_for("migration.map_columns", bid=bid))
    rows = json.loads(batch.rows_json) if batch.rows_json else []
    return render_template("migration/view.html", batch=batch, rows=rows,
                           spec=services.KINDS[batch.kind])


@bp.route("/<int:bid>/commit", methods=["POST"])
@requires("accounting.period.close")  # owner approves the opening balances (MIG-01)
def commit(bid):
    batch = db.get_or_404(ImportBatch, bid)
    if batch.status == "committed":
        flash("سبق ترحيل هذه الدفعة.", "warning")
        return redirect(url_for("migration.view", bid=bid))
    if batch.error_count > 0:
        flash("لا يمكن الترحيل مع وجود أخطاء. صحّح الملف وأعد الرفع.", "error")
        return redirect(url_for("migration.view", bid=bid))
    try:
        n = services.commit(batch, user_id=current_user.id)
        db.session.commit()
        flash(f"تم ترحيل {n} سجلًا وإنشاء الأرصدة الافتتاحية.", "success")
    except Exception as e:  # all-or-nothing (MIG-03.5)
        db.session.rollback()
        flash(f"فشل الترحيل ولم يُحفظ شيء: {e}", "error")
    return redirect(url_for("migration.view", bid=bid))
