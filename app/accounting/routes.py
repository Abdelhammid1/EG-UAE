"""Accounting screens: chart of accounts, journal, periods, trial balance, FX."""
from __future__ import annotations

from datetime import date, datetime

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.accounting import posting, services
from app.accounting.forms import AccountForm, PeriodForm, RateForm
from app.accounting.models import (
    Account,
    AccountingPeriod,
    ExchangeRate,
    JournalEntry,
)
from app.core import fx
from app.core.models import Branch, Currency
from app.core.permissions import requires
from app.extensions import db

bp = Blueprint("accounting", __name__)


@bp.before_request
@login_required
def _guard():
    pass


# --- Chart of accounts ----------------------------------------------------
@bp.route("/accounts")
@requires("accounting.journal.create")
def accounts():
    all_accounts = db.session.scalars(
        db.select(Account).order_by(Account.code)
    ).all()
    return render_template("accounting/accounts.html", accounts=all_accounts)


@bp.route("/accounts/new", methods=["GET", "POST"])
@requires("accounting.journal.create")
def account_new():
    form = AccountForm()
    form.parent_id.choices = [(0, "— لا يوجد —")] + [
        (a.id, f"{a.code} · {a.name_ar}")
        for a in db.session.scalars(db.select(Account).order_by(Account.code))
    ]
    if form.validate_on_submit():
        acc = Account(
            code=form.code.data.strip(),
            name_ar=form.name_ar.data.strip(),
            name_en=(form.name_en.data or "").strip(),
            type=form.type.data,
            parent_id=form.parent_id.data or None,
            is_postable=form.is_postable.data == "1",
            created_by_id=current_user.id,
        )
        db.session.add(acc)
        db.session.commit()
        flash("تم إنشاء الحساب.", "success")
        return redirect(url_for("accounting.accounts"))
    return render_template("accounting/account_form.html", form=form, title="حساب جديد")


# --- Journal --------------------------------------------------------------
@bp.route("/journal")
@requires("accounting.journal.create")
def journal():
    entries = db.session.scalars(
        db.select(JournalEntry).order_by(JournalEntry.id.desc()).limit(100)
    ).all()
    return render_template("accounting/journal.html", entries=entries)


@bp.route("/journal/new")
@requires("accounting.journal.create")
def journal_new():
    postable = db.session.scalars(
        db.select(Account).filter_by(is_postable=True, is_active=True).order_by(Account.code)
    ).all()
    currencies = db.session.scalars(db.select(Currency)).all()
    branches = db.session.scalars(db.select(Branch)).all()
    return render_template(
        "accounting/journal_form.html",
        accounts=postable, currencies=currencies, branches=branches,
        book_currency=_book_currency(), today=date.today().isoformat(),
    )


@bp.route("/journal", methods=["POST"])
@requires("accounting.journal.create")
def journal_create():
    """Create a DRAFT manual entry from dynamic line rows."""
    try:
        entry_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        entry_date = date.today()
    branch_id = request.form.get("branch_id", type=int) or None
    memo = request.form.get("memo", "").strip()

    account_ids = request.form.getlist("account_id")
    sides = request.form.getlist("side")
    amounts = request.form.getlist("amount")
    currencies = request.form.getlist("currency")
    rates = request.form.getlist("rate")
    line_memos = request.form.getlist("line_memo")

    lines = []
    try:
        for i, acc in enumerate(account_ids):
            if not acc or not amounts[i] or float(amounts[i] or 0) == 0:
                continue
            manual_rate = rates[i] if i < len(rates) and rates[i].strip() else None
            lines.append(posting.line(
                account_id=int(acc),
                currency=currencies[i],
                amount=amounts[i],
                side=sides[i],
                fx_rate=manual_rate,
                memo=(line_memos[i] if i < len(line_memos) else None) or None,
            ))
    except fx.RateUnavailableError as e:
        flash(str(e), "error")
        return redirect(url_for("accounting.journal_new"))

    if not lines:
        flash("أضف أطرافًا للقيد.", "error")
        return redirect(url_for("accounting.journal_new"))

    entry = posting.build_entry(
        entry_date=entry_date, branch_id=branch_id, lines=lines,
        memo=memo, user_id=current_user.id,
    )
    if not entry.is_balanced:
        db.session.rollback()
        flash(
            f"القيد غير متزن: مدين {entry.total_debit_book} ≠ دائن {entry.total_credit_book}.",
            "error",
        )
        return redirect(url_for("accounting.journal_new"))

    db.session.commit()
    flash("تم حفظ القيد كمسودة. يعتمده مستخدم آخر (فصل المهام).", "success")
    return redirect(url_for("accounting.journal_view", entry_id=entry.id))


@bp.route("/journal/<int:entry_id>")
@requires("accounting.journal.create")
def journal_view(entry_id):
    entry = db.get_or_404(JournalEntry, entry_id)
    return render_template("accounting/journal_view.html", entry=entry)


@bp.route("/journal/<int:entry_id>/post", methods=["POST"])
@requires("accounting.journal.post")
def journal_post(entry_id):
    entry = db.get_or_404(JournalEntry, entry_id)
    try:
        posting.post_entry(entry, user_id=current_user.id)
        db.session.commit()
        flash(f"تم ترحيل القيد {entry.number}.", "success")
    except (posting.UnbalancedEntryError, posting.ClosedPeriodError,
            posting.SeparationOfDutiesError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("accounting.journal_view", entry_id=entry_id))


@bp.route("/journal/<int:entry_id>/reverse", methods=["POST"])
@requires("accounting.journal.post")
def journal_reverse(entry_id):
    entry = db.get_or_404(JournalEntry, entry_id)
    try:
        rev = posting.reverse_entry(entry, user_id=current_user.id)
        db.session.commit()
        flash(f"تم إنشاء قيد عكسي {rev.number}.", "success")
        return redirect(url_for("accounting.journal_view", entry_id=rev.id))
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("accounting.journal_view", entry_id=entry_id))


# --- Periods --------------------------------------------------------------
@bp.route("/periods", methods=["GET", "POST"])
@requires("accounting.journal.create")
def periods():
    form = PeriodForm()
    if form.validate_on_submit():
        if not current_user.has_permission("accounting.period.close"):
            abort(403)
        db.session.add(AccountingPeriod(
            name=form.name.data.strip(),
            start_date=form.start_date.data,
            end_date=form.end_date.data,
            created_by_id=current_user.id,
        ))
        db.session.commit()
        flash("تم إنشاء الفترة.", "success")
        return redirect(url_for("accounting.periods"))
    all_periods = db.session.scalars(
        db.select(AccountingPeriod).order_by(AccountingPeriod.start_date.desc())
    ).all()
    return render_template("accounting/periods.html", periods=all_periods, form=form)


@bp.route("/periods/<int:period_id>/close", methods=["POST"])
@requires("accounting.period.close")
def period_close(period_id):
    period = db.get_or_404(AccountingPeriod, period_id)
    # Owner approval (spec §13.6). Owner approving own action is allowed here.
    approver = current_user.id if current_user.has_role("owner") else None
    try:
        services.close_period(period, user_id=current_user.id, approved_by_id=approver)
        db.session.commit()
        flash(f"تم إقفال الفترة {period.name} باعتماد المالك.", "success")
    except services.OwnerApprovalRequired as e:
        db.session.rollback()
        flash(str(e) + " (يلزم أن يقوم المالك بالإقفال).", "error")
    return redirect(url_for("accounting.periods"))


@bp.route("/periods/<int:period_id>/reopen", methods=["POST"])
@requires("accounting.period.close")
def period_reopen(period_id):
    period = db.get_or_404(AccountingPeriod, period_id)
    approver = current_user.id if current_user.has_role("owner") else None
    try:
        services.reopen_period(period, user_id=current_user.id, approved_by_id=approver)
        db.session.commit()
        flash(f"تمت إعادة فتح الفترة {period.name}.", "success")
    except services.OwnerApprovalRequired as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("accounting.periods"))


# --- Trial balance --------------------------------------------------------
@bp.route("/trial-balance")
@requires("reports.financial")
def trial_balance():
    branch_id = request.args.get("branch_id", type=int)
    tb = services.trial_balance(branch_id=branch_id or None)
    branches = db.session.scalars(db.select(Branch)).all()
    return render_template(
        "accounting/trial_balance.html", tb=tb, branches=branches,
        branch_id=branch_id, book_currency=_book_currency(),
    )


# --- FX rates -------------------------------------------------------------
@bp.route("/fx", methods=["GET", "POST"])
@requires("accounting.journal.create")
def fx_rates():
    form = RateForm()
    codes = [(c.code, f"{c.code} · {c.name_ar}")
             for c in db.session.scalars(db.select(Currency))]
    form.from_currency.choices = codes
    form.to_currency.choices = codes
    if form.validate_on_submit():
        try:
            fx.record_rate(
                form.from_currency.data, form.to_currency.data,
                form.rate.data, source="manual", user_id=current_user.id,
            )
            db.session.commit()
            flash("تم تسجيل سعر الصرف.", "success")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("accounting.fx_rates"))
    rates = db.session.scalars(
        db.select(ExchangeRate).order_by(ExchangeRate.at.desc()).limit(50)
    ).all()
    return render_template("accounting/fx.html", form=form, rates=rates)


# --- helpers --------------------------------------------------------------
def _book_currency():
    from flask import current_app
    return current_app.config["BOOK_CURRENCY"]
