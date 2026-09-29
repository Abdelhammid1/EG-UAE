"""Reporting screens (spec §14)."""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, abort, render_template, request
from flask_login import login_required

from app.core.permissions import requires
from app.extensions import db
from app.reports import services

bp = Blueprint("reports", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _range():
    """Read start/end from the query, defaulting to year-to-date."""
    today = date.today()
    start = _parse(request.args.get("start"), date(today.year, 1, 1))
    end = _parse(request.args.get("end"), today)
    branch_id = request.args.get("branch_id", type=int) or None
    return start, end, branch_id


def _parse(s, default):
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return default


def _branches():
    from app.core.models import Branch
    return db.session.scalars(db.select(Branch)).all()


@bp.route("/dashboard")
@requires("reports.financial")
def dashboard():
    branch_id = request.args.get("branch_id", type=int) or None
    data = services.dashboard(branch_id)
    # per-branch snapshot for comparison
    branches = _branches()
    per_branch = [{"branch": b, **services.dashboard(b.id)} for b in branches]
    from app.inventory.models import Sale
    recent = db.session.scalars(
        db.select(Sale).order_by(Sale.id.desc()).limit(8)).all()
    return render_template("reports/dashboard.html", d=data, per_branch=per_branch,
                           branches=branches, branch_id=branch_id, recent=recent)


@bp.route("/income-statement")
@requires("reports.financial")
def income_statement():
    start, end, branch_id = _range()
    data = services.income_statement(start=start, end=end, branch_id=branch_id)
    return render_template("reports/income_statement.html", r=data, start=start,
                           end=end, branch_id=branch_id, branches=_branches())


@bp.route("/balance-sheet")
@requires("reports.financial")
def balance_sheet():
    start, end, branch_id = _range()
    data = services.balance_sheet(as_of=end, branch_id=branch_id)
    return render_template("reports/balance_sheet.html", r=data, as_of=end,
                           branch_id=branch_id, branches=_branches())


@bp.route("/general-ledger")
@requires("reports.financial")
def general_ledger():
    from app.accounting.models import Account
    accounts = db.session.scalars(
        db.select(Account).filter_by(is_postable=True).order_by(Account.code)).all()
    start, end, branch_id = _range()
    account_id = request.args.get("account_id", type=int)
    rows = services.general_ledger(account_id, start=start, end=end,
                                   branch_id=branch_id) if account_id else []
    account = db.session.get(Account, account_id) if account_id else None
    return render_template("reports/general_ledger.html", accounts=accounts, rows=rows,
                           account=account, account_id=account_id, start=start, end=end)


@bp.route("/sales")
@requires("reports.branch")
def sales():
    start, end, branch_id = _range()
    rows = services.sales_by_product(start=start, end=end, branch_id=branch_id)
    totals = {
        "revenue": sum((r["revenue"] for r in rows), 0),
        "cogs": sum((r["cogs"] for r in rows), 0),
        "margin": sum((r["margin"] for r in rows), 0),
    }
    return render_template("reports/sales.html", rows=rows, totals=totals, start=start,
                           end=end, branch_id=branch_id, branches=_branches())


@bp.route("/tax")
@requires("reports.financial")
def tax():
    start, end, branch_id = _range()
    data = services.tax_report(start=start, end=end, branch_id=branch_id)
    return render_template("reports/tax.html", r=data, start=start, end=end,
                           branch_id=branch_id, branches=_branches())


@bp.route("/fx")
@requires("reports.financial")
def fx():
    from app.accounting.models import AccountingPeriod
    periods = db.session.scalars(
        db.select(AccountingPeriod).order_by(AccountingPeriod.start_date.desc())).all()
    start, end, branch_id = _range()
    # realized FX = movements on the fx gain/loss accounts in the window
    from app.accounting.posting import account_for, MappingMissingError
    realized_gain = realized_loss = None
    try:
        totals = services.ledger_totals(start=start, end=end, branch_id=branch_id)
        g = totals.get(account_for("fx.gain"))
        l = totals.get(account_for("fx.loss"))
        realized_gain = (g[2] - g[1]) if g else 0
        realized_loss = (l[1] - l[2]) if l else 0
    except MappingMissingError:
        pass
    return render_template("reports/fx.html", periods=periods, start=start, end=end,
                           realized_gain=realized_gain, realized_loss=realized_loss)


@bp.route("/fx/revalue/<int:period_id>", methods=["POST"])
@requires("accounting.period.close")
def fx_revalue(period_id):
    from flask import flash, redirect, url_for
    from flask_login import current_user
    from app.accounting.models import AccountingPeriod
    period = db.get_or_404(AccountingPeriod, period_id)
    entry = services.revalue_unrealized(period=period, user_id=current_user.id)
    db.session.commit()
    if entry:
        flash(f"تم تسجيل قيد إعادة التقييم {entry.number} وعكسه في الفترة التالية.", "success")
    else:
        flash("لا توجد أرصدة بعملات أجنبية لإعادة تقييمها.", "info")
    return redirect(url_for("reports.fx"))


@bp.route("/daily-close")
@requires("reports.branch")
def daily_close():
    from datetime import datetime
    day = request.args.get("day")
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else None
    except ValueError:
        d = None
    branch_id = request.args.get("branch_id", type=int) or None
    data = services.daily_close(day=d, branch_id=branch_id)
    return render_template("reports/daily_close.html", d=data, branches=_branches(),
                           branch_id=branch_id)


@bp.route("/exceptions")
@requires("reports.financial")
def exceptions():
    return render_template("reports/exceptions.html", x=services.exceptions())


# --- async (background) large reports -------------------------------------
_ASYNC_REPORTS = {
    "general_ledger": "دفتر الأستاذ الكامل (كل الحسابات)",
    "journal": "كل القيود اليومية",
    "audit": "سجل التدقيق الكامل",
}


@bp.route("/async")
@requires("reports.financial")
def async_index():
    from flask_login import current_user
    from app.core import jobs
    return render_template("reports/async.html", reports=_ASYNC_REPORTS,
                           jobs=jobs.for_user(current_user.id))


@bp.route("/async/run", methods=["POST"])
@requires("reports.financial")
def async_run():
    from flask import flash, redirect, url_for
    from flask_login import current_user
    from app.core import jobs
    kind = request.form.get("kind")
    if kind not in _ASYNC_REPORTS:
        abort(400)
    builder = _async_builders()[kind]
    jobs.submit(_ASYNC_REPORTS[kind], builder, user_id=current_user.id,
                download_name=f"{kind}.xlsx")
    flash("بدأ إنشاء التقرير في الخلفية — سيظهر رابط التنزيل هنا عند اكتماله.",
          "success")
    return redirect(url_for("reports.async_index"))


@bp.route("/async/<job_id>/download")
@requires("reports.financial")
def async_download(job_id):
    from flask import send_file
    from flask_login import current_user
    from app.core import jobs
    job = jobs.get(job_id)
    if not job or (job.get("user_id") not in (None, current_user.id)):
        abort(404)
    if job["status"] != "done" or not job.get("path"):
        abort(409)
    return send_file(
        job["path"], as_attachment=True,
        download_name=job.get("download_name", "report.xlsx"),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _async_builders():
    """Map of report kind -> zero-arg function returning xlsx bytes. Defined
    lazily so heavy imports only load when a report is actually generated."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font

    def _wb(title, headers, row_iter):
        wb = Workbook()
        ws = wb.active
        ws.title = title[:31]
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            ws.cell(row=1, column=c).font = Font(bold=True)
        for r in row_iter:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def general_ledger():
        from decimal import Decimal
        from app.accounting.models import Account
        from app.core.money import to_decimal
        rows = []
        for acc in db.session.scalars(
                db.select(Account).filter_by(is_postable=True).order_by(Account.code)):
            for m in services.general_ledger(acc.id):
                rows.append([acc.code, acc.name_ar, m["entry"].date.isoformat(),
                             m["entry"].number, m["entry"].memo,
                             float(to_decimal(m["line"].debit_book)),
                             float(to_decimal(m["line"].credit_book)),
                             float(m["balance"])])
        return _wb("دفتر الأستاذ",
                   ["الرمز", "الحساب", "التاريخ", "القيد", "البيان", "مدين",
                    "دائن", "الرصيد"], rows)

    def journal():
        from app.accounting.models import JournalEntry
        rows = [[e.number, e.date.isoformat(), e.memo, float(e.total_debit_book),
                 e.status] for e in db.session.scalars(
                    db.select(JournalEntry).order_by(JournalEntry.id))]
        return _wb("القيود", ["الرقم", "التاريخ", "البيان", "القيمة", "الحالة"], rows)

    def audit():
        from app.core.audit import AuditLog
        rows = [[e.at.isoformat() if e.at else "", e.user_name, e.action, e.entity,
                 e.old_value, e.new_value] for e in db.session.scalars(
                    db.select(AuditLog).order_by(AuditLog.at.desc()))]
        return _wb("سجل التدقيق",
                   ["الوقت", "المستخدم", "العملية", "الكيان", "القديم", "الجديد"],
                   rows)

    return {"general_ledger": general_ledger, "journal": journal, "audit": audit}
