"""Party screens (spec §M4, PTY-01..09)."""
from __future__ import annotations

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core.permissions import requires
from app.extensions import db
from app.parties import services
from app.parties.models import Party
from app.treasury.models import Treasury

bp = Blueprint("parties", __name__)


@bp.before_request
@login_required
def _guard():
    pass


@bp.route("/")
@requires("treasury.transfer")
def index():
    parties = db.session.scalars(db.select(Party).order_by(Party.name_ar)).all()
    return render_template("parties/index.html", parties=parties,
                           balance=services.party_balance)


@bp.route("/new", methods=["GET", "POST"])
@requires("treasury.transfer")
def new():
    from app.core.models import Branch
    if request.method == "POST":
        branch = db.session.scalar(db.select(Branch))
        types = ",".join(request.form.getlist("types")) or "person"
        services.create_party(
            name_ar=request.form["name_ar"].strip(),
            phone=request.form.get("phone", ""),
            address=request.form.get("address", ""), types=types,
            branch_id=branch.id if branch else None,
            notes=request.form.get("notes", ""), user_id=current_user.id)
        db.session.commit()
        flash("تم إنشاء الطرف.", "success")
        return redirect(url_for("parties.index"))
    return render_template("parties/form.html")


@bp.route("/<int:pid>")
@requires("treasury.transfer")
def view(pid):
    from app.accounting.models import Account
    from app.core.models import Currency
    from app.treasury.services import statement
    party = db.get_or_404(Party, pid)
    accounts = []
    for pa in party.accounts:
        accounts.append({"pa": pa, "balance": services.party_balance(party, pa.currency_code),
                         "rows": statement(pa.account_id)})
    treasuries = db.session.scalars(db.select(Treasury).filter_by(is_active=True)).all()
    currencies = db.session.scalars(db.select(Currency)).all()
    expense_accounts = db.session.scalars(
        db.select(Account).filter_by(type="expense", is_postable=True)).all()
    other_parties = db.session.scalars(
        db.select(Party).filter(Party.id != pid)).all()
    return render_template("parties/view.html", party=party, accounts=accounts,
                           treasuries=treasuries, currencies=currencies,
                           expense_accounts=expense_accounts, other_parties=other_parties,
                           summary=services.party_summary(party))


@bp.route("/<int:pid>/pay", methods=["POST"])
@requires("treasury.transfer")
def pay(pid):
    party = db.get_or_404(Party, pid)
    t = db.get_or_404(Treasury, request.form.get("treasury_id", type=int))
    from app.core import fx
    try:
        services.pay_to_party(
            party=party, from_treasury=t, amount=request.form["amount"],
            party_currency=request.form.get("party_currency") or None,
            rate=request.form.get("rate") or None,
            memo=request.form.get("memo"), user_id=current_user.id)
        db.session.commit()
        flash("تم صرف المبلغ للطرف.", "success")
    except (fx.RateUnavailableError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("parties.view", pid=pid))


@bp.route("/<int:pid>/refund", methods=["POST"])
@requires("treasury.transfer")
def refund(pid):
    party = db.get_or_404(Party, pid)
    t = db.get_or_404(Treasury, request.form.get("treasury_id", type=int))
    from app.core import fx
    try:
        services.refund_from_party(
            party=party, to_treasury=t, amount=request.form["amount"],
            party_currency=request.form.get("party_currency") or None,
            rate=request.form.get("rate") or None,
            memo=request.form.get("memo"), user_id=current_user.id)
        db.session.commit()
        flash("تم استرداد المبلغ.", "success")
    except (services.InsufficientPartyBalance, fx.RateUnavailableError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("parties.view", pid=pid))


@bp.route("/<int:pid>/spend", methods=["POST"])
@requires("treasury.transfer")
def spend(pid):
    party = db.get_or_404(Party, pid)
    try:
        services.party_spend(
            party=party, expense_account_id=request.form.get("expense_account_id", type=int),
            amount=request.form["amount"], currency=request.form["currency"],
            memo=request.form.get("memo"), user_id=current_user.id)
        db.session.commit()
        flash("تم تسجيل المصروف من رصيد الطرف.", "success")
    except services.InsufficientPartyBalance as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("parties.view", pid=pid))


@bp.route("/<int:pid>/transfer", methods=["POST"])
@requires("treasury.transfer")
def transfer(pid):
    party = db.get_or_404(Party, pid)
    to_party = db.get_or_404(Party, request.form.get("to_party_id", type=int))
    try:
        services.transfer_between_parties(
            from_party=party, to_party=to_party, amount=request.form["amount"],
            currency=request.form["currency"], memo=request.form.get("memo"),
            user_id=current_user.id)
        db.session.commit()
        flash("تم التحويل بين الطرفين.", "success")
    except services.InsufficientPartyBalance as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("parties.view", pid=pid))


@bp.route("/summary")
@requires("treasury.transfer")
def summary():
    return render_template("parties/summary.html",
                           data=services.all_parties_summary())


@bp.route("/where-is-money")
@requires("reports.financial")
def where_is_money():
    from app.core.models import Currency
    display = request.args.get("currency", "AED")
    data = services.where_is_money(display_currency=display)
    currencies = db.session.scalars(db.select(Currency)).all()
    return render_template("parties/where_is_money.html", d=data,
                           currencies=currencies, display=display)
