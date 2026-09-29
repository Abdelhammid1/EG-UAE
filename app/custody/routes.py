"""Consignment / العهد screens (spec §12)."""
from __future__ import annotations

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core.permissions import requires
from app.extensions import db
from app.custody import services
from app.custody.models import Custody
from app.treasury.models import Treasury

bp = Blueprint("custody", __name__)


@bp.before_request
@login_required
def _guard():
    pass


@bp.route("/")
@requires("custody.settle")
def index():
    items = db.session.scalars(db.select(Custody).order_by(Custody.id.desc())).all()
    avail = {c.id: services.available(c) for c in items}
    return render_template("custody/index.html", custodies=items, avail=avail,
                           alerts=services.departed_rep_alerts())


@bp.route("/new", methods=["GET", "POST"])
@requires("custody.settle")
def new():
    from app.core.models import Branch, Currency
    from app.auth.models import User
    if request.method == "POST":
        branch = db.session.scalar(db.select(Branch))
        services.create_custody(
            name_ar=request.form["name_ar"].strip(),
            branch_id=branch.id if branch else None,
            currency_code=request.form.get("currency_code", "AED"),
            rep_user_id=request.form.get("rep_user_id", type=int) or None,
            user_id=current_user.id)
        db.session.commit()
        flash("تم إنشاء العهدة.", "success")
        return redirect(url_for("custody.index"))
    reps = db.session.scalars(db.select(User)).all()
    currencies = db.session.scalars(db.select(Currency)).all()
    return render_template("custody/form.html", reps=reps, currencies=currencies)


@bp.route("/<int:cid>")
@requires("custody.settle")
def view(cid):
    from app.custody.models import CustodyTxn
    from app.accounting.models import Account
    c = db.get_or_404(Custody, cid)
    txns = db.session.scalars(
        db.select(CustodyTxn).filter_by(custody_id=cid)
        .order_by(CustodyTxn.id)).all()
    treasuries = db.session.scalars(
        db.select(Treasury).filter_by(currency_code=c.currency_code,
                                      is_active=True)).all()
    expense_accounts = db.session.scalars(
        db.select(Account).filter_by(type="expense", is_postable=True)).all()
    return render_template("custody/view.html", custody=c, txns=txns,
                           available=services.available(c), treasuries=treasuries,
                           expense_accounts=expense_accounts)


@bp.route("/<int:cid>/issue", methods=["POST"])
@requires("custody.settle")
def issue(cid):
    c = db.get_or_404(Custody, cid)
    t = db.get_or_404(Treasury, request.form.get("treasury_id", type=int))
    try:
        services.issue(custody=c, from_treasury=t, amount=request.form["amount"],
                       memo=request.form.get("memo"), user_id=current_user.id)
        db.session.commit()
        flash("تم صرف العهدة.", "success")
    except (ValueError, services.CustodyClosedError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("custody.view", cid=cid))


@bp.route("/<int:cid>/expense", methods=["POST"])
@requires("custody.settle")
def expense(cid):
    c = db.get_or_404(Custody, cid)
    try:
        txn = services.spend_expense(
            custody=c, expense_account_id=request.form.get("expense_account_id", type=int),
            amount=request.form["amount"], memo=request.form.get("memo"),
            user_id=current_user.id)
        db.session.commit()
        if txn.status == "pending":
            flash("المبلغ يتجاوز حد الاعتماد — سُجِّل بانتظار اعتماد المالك.", "info")
        else:
            flash("تم تسجيل المصروف.", "success")
    except (services.OverspendError, services.CustodyClosedError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("custody.view", cid=cid))


@bp.route("/expense/<int:txn_id>/approve", methods=["POST"])
@requires("custody.settle")
def expense_approve(txn_id):
    from app.custody.models import CustodyTxn
    txn = db.get_or_404(CustodyTxn, txn_id)
    try:
        services.approve_expense(txn, user_id=current_user.id)
        db.session.commit()
        flash("تم اعتماد مصروف العهدة وترحيله.", "success")
    except (services.ApprovalError, services.OverspendError,
            services.CustodyClosedError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.form.get("next") or url_for("custody.view",
                                                        cid=txn.custody_id))


@bp.route("/expense/<int:txn_id>/reject", methods=["POST"])
@requires("custody.settle")
def expense_reject(txn_id):
    from app.custody.models import CustodyTxn
    txn = db.get_or_404(CustodyTxn, txn_id)
    try:
        services.reject_expense(txn, user_id=current_user.id)
        db.session.commit()
        flash("تم رفض مصروف العهدة.", "info")
    except services.ApprovalError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.form.get("next") or url_for("custody.view",
                                                        cid=txn.custody_id))


@bp.route("/<int:cid>/close", methods=["POST"])
@requires("custody.settle")
def close(cid):
    c = db.get_or_404(Custody, cid)
    t = db.get_or_404(Treasury, request.form.get("treasury_id", type=int))
    try:
        services.close(custody=c, to_treasury=t, user_id=current_user.id)
        db.session.commit()
        flash("تم إغلاق العهدة وإرجاع الرصيد.", "success")
    except services.CustodyClosedError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("custody.view", cid=cid))
