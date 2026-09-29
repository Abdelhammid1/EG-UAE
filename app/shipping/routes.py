"""Inter-warehouse transfer screens (spec §8)."""
from __future__ import annotations

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core.money import to_decimal
from app.core.permissions import requires
from app.extensions import db
from app.shipping import services
from app.shipping.models import TransferPermit
from app.treasury.models import BankAccount, Treasury

bp = Blueprint("shipping", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _pay_sources():
    out = []
    for t in db.session.scalars(db.select(Treasury).filter_by(is_active=True)):
        out.append((t.account_id, f"خزينة: {t.name_ar}"))
    for b in db.session.scalars(db.select(BankAccount).filter_by(is_active=True)):
        out.append((b.account_id, f"بنك: {b.name_ar}"))
    return out


@bp.route("/")
@requires("warehouse.transfer")
def index():
    permits = db.session.scalars(
        db.select(TransferPermit).order_by(TransferPermit.id.desc())).all()
    return render_template("shipping/index.html", permits=permits)


@bp.route("/new", methods=["GET", "POST"])
@requires("warehouse.transfer")
def new():
    from app.inventory.models import Product, Warehouse
    warehouses = db.session.scalars(db.select(Warehouse)).all()
    products = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    if request.method == "POST":
        src = db.get_or_404(Warehouse, request.form.get("from_warehouse_id", type=int))
        dst = db.get_or_404(Warehouse, request.form.get("to_warehouse_id", type=int))
        method = request.form.get("allocation_method", "value")
        pids = request.form.getlist("product_id")
        qtys = request.form.getlist("qty")
        lines = []
        for i, pid in enumerate(pids):
            if not pid or not qtys[i] or to_decimal(qtys[i]) <= 0:
                continue
            lines.append((db.get_or_404(Product, int(pid)), qtys[i]))
        if not lines:
            flash("أضف أصنافًا.", "error")
            return redirect(request.url)
        try:
            permit = services.create_permit(from_warehouse=src, to_warehouse=dst,
                                            lines=lines, allocation_method=method,
                                            user_id=current_user.id)
            db.session.commit()
            return redirect(url_for("shipping.view", pid=permit.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            return redirect(request.url)
    return render_template("shipping/new.html", warehouses=warehouses, products=products)


@bp.route("/<int:pid>")
@requires("warehouse.transfer")
def view(pid):
    permit = db.get_or_404(TransferPermit, pid)
    return render_template("shipping/view.html", permit=permit,
                           sources=_pay_sources())


@bp.route("/<int:pid>/send", methods=["POST"])
@requires("warehouse.transfer")
def send(pid):
    permit = db.get_or_404(TransferPermit, pid)
    try:
        services.send_permit(permit, user_id=current_user.id)
        db.session.commit()
        flash("تم إرسال الشحنة — الكميات في الطريق.", "success")
    except (services.TransferStateError, services.NotEnoughStockError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("shipping.view", pid=pid))


@bp.route("/<int:pid>/expense", methods=["POST"])
@requires("warehouse.transfer")
def expense(pid):
    permit = db.get_or_404(TransferPermit, pid)
    try:
        services.add_expense(
            permit, kind=request.form.get("kind", "shipping"),
            amount_book=request.form["amount"],
            paid_from_account_id=request.form.get("paid_from_account_id", type=int),
            note=request.form.get("note"), user_id=current_user.id)
        db.session.commit()
        flash("تم تسجيل المصروف وتوزيعه.", "success")
    except services.TransferStateError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("shipping.view", pid=pid))


@bp.route("/<int:pid>/receive", methods=["POST"])
@requires("warehouse.transfer")
def receive(pid):
    permit = db.get_or_404(TransferPermit, pid)
    received = {}
    for line in permit.lines:
        v = request.form.get(f"recv_{line.id}")
        if v not in (None, ""):
            received[line.id] = v
    partial = request.form.get("partial") == "1"
    try:
        services.receive_permit(permit, received=received, partial=partial,
                                user_id=current_user.id)
        db.session.commit()
        msg = ("تم استلام جزء من الشحنة — المتبقي ما زال في الطريق."
               if partial and permit.status == "sent"
               else "تم استلام الشحنة في المخزن المستلم.")
        flash(msg, "success")
    except services.TransferStateError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("shipping.view", pid=pid))


@bp.route("/<int:pid>/close", methods=["POST"])
@requires("warehouse.transfer")
def close(pid):
    permit = db.get_or_404(TransferPermit, pid)
    try:
        services.close_permit(permit, user_id=current_user.id)
        db.session.commit()
        flash("تم إغلاق الإذن.", "success")
    except services.TransferStateError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("shipping.view", pid=pid))


@bp.route("/<int:pid>/cancel", methods=["POST"])
@requires("warehouse.transfer")
def cancel(pid):
    permit = db.get_or_404(TransferPermit, pid)
    try:
        services.cancel_permit(permit, user_id=current_user.id)
        db.session.commit()
        flash("تم إلغاء الإذن.", "success")
    except services.TransferStateError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("shipping.view", pid=pid))
