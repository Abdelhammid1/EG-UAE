"""Suppliers & purchasing screens (spec §9)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core.money import to_decimal
from app.core.permissions import requires
from app.extensions import db
from app.purchasing import services
from app.purchasing.models import PurchaseInvoice, Supplier, SupplierPayment
from app.treasury.models import BankAccount, Treasury

bp = Blueprint("purchasing", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _pay_sources():
    """Treasuries + bank accounts as (account_id, label, currency)."""
    out = []
    for t in db.session.scalars(db.select(Treasury).filter_by(is_active=True)):
        out.append((t.account_id, f"خزينة: {t.name_ar}", t.currency_code))
    for b in db.session.scalars(db.select(BankAccount).filter_by(is_active=True)):
        out.append((b.account_id, f"بنك: {b.name_ar}", b.currency_code))
    return out


# --- suppliers ------------------------------------------------------------
@bp.route("/suppliers")
@requires("purchase.invoice.create")
def suppliers():
    items = db.session.scalars(db.select(Supplier).order_by(Supplier.name_ar)).all()
    bal = {s.id: services.ap_balance(s) for s in items}
    return render_template("purchasing/suppliers.html", suppliers=items, bal=bal)


@bp.route("/suppliers/new", methods=["GET", "POST"])
@requires("purchase.invoice.create")
def supplier_new():
    from app.core.models import Currency, Branch
    if request.method == "POST":
        branch = db.session.scalar(db.select(Branch))
        services.create_supplier(
            name_ar=request.form["name_ar"].strip(),
            phone=request.form.get("phone", ""),
            currency_code=request.form.get("currency_code", "AED"),
            payment_terms_days=request.form.get("payment_terms_days", type=int) or 0,
            opening_balance=request.form.get("opening_balance") or 0,
            branch_id=branch.id if branch else None, user_id=current_user.id)
        db.session.commit()
        flash("تم إنشاء المورد.", "success")
        return redirect(url_for("purchasing.suppliers"))
    currencies = db.session.scalars(db.select(Currency)).all()
    return render_template("purchasing/supplier_form.html", currencies=currencies)


@bp.route("/suppliers/<int:sid>")
@requires("purchase.invoice.create")
def supplier_view(sid):
    s = db.get_or_404(Supplier, sid)
    from app.treasury.services import statement
    rows = statement(s.account_id)
    invoices = db.session.scalars(
        db.select(PurchaseInvoice).filter_by(supplier_id=sid)
        .order_by(PurchaseInvoice.id.desc())).all()
    return render_template("purchasing/supplier_view.html", supplier=s, rows=rows,
                           balance=services.ap_balance(s), invoices=invoices,
                           outstanding=services.invoice_outstanding)


# --- purchase invoice -----------------------------------------------------
@bp.route("/invoices/new", methods=["GET", "POST"])
@requires("purchase.invoice.create")
def invoice_new():
    from app.inventory.models import Product, Warehouse
    from app.custody.models import Custody
    products = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    warehouses = db.session.scalars(db.select(Warehouse)).all()
    suppliers_ = db.session.scalars(db.select(Supplier).order_by(Supplier.name_ar)).all()
    custodies = db.session.scalars(
        db.select(Custody).filter_by(status="open")).all()

    if request.method == "POST":
        warehouse = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
        supplier = db.session.get(Supplier, request.form.get("supplier_id", type=int))
        payment_type = request.form.get("payment_type")
        pay_from = request.form.get("pay_from_account_id", type=int)
        custody = db.session.get(Custody, request.form.get("custody_id", type=int))
        due = request.form.get("due_date")
        due_date = None
        if due:
            try:
                due_date = datetime.strptime(due, "%Y-%m-%d").date()
            except ValueError:
                pass
        pids = request.form.getlist("product_id")
        qtys = request.form.getlist("qty")
        costs = request.form.getlist("cost")
        lines = []
        for i, pid in enumerate(pids):
            if not pid or not qtys[i] or to_decimal(qtys[i]) <= 0:
                continue
            lines.append((db.get_or_404(Product, int(pid)), qtys[i], costs[i]))
        if not lines:
            flash("أضف أصنافًا للفاتورة.", "error")
            return redirect(request.url)
        if payment_type == "credit" and supplier is None:
            flash("الشراء الآجل يتطلب مورّدًا.", "error")
            return redirect(request.url)
        try:
            invoice = services.create_purchase_invoice(
                supplier=supplier, warehouse=warehouse, lines=lines,
                payment_type=payment_type, pay_from_account_id=pay_from,
                custody=custody, due_date=due_date, user_id=current_user.id)
            db.session.commit()
            flash(f"تم تسجيل فاتورة الشراء {invoice.number}.", "success")
            return redirect(url_for("purchasing.invoice_view", inv_id=invoice.id))
        except services.InsufficientCustodyError as e:
            db.session.rollback()
            flash(str(e), "error")
            return redirect(request.url)

    return render_template("purchasing/invoice_form.html", products=products,
                           warehouses=warehouses, suppliers=suppliers_,
                           custodies=custodies, sources=_pay_sources())


@bp.route("/invoices/<int:inv_id>")
@requires("purchase.invoice.create")
def invoice_view(inv_id):
    invoice = db.get_or_404(PurchaseInvoice, inv_id)
    return render_template("purchasing/invoice_view.html", invoice=invoice,
                           outstanding=services.invoice_outstanding(invoice))


@bp.route("/invoices")
@requires("purchase.invoice.create")
def invoices():
    items = db.session.scalars(
        db.select(PurchaseInvoice).order_by(PurchaseInvoice.id.desc()).limit(100)).all()
    return render_template("purchasing/invoices.html", invoices=items,
                           outstanding=services.invoice_outstanding)


# --- payment --------------------------------------------------------------
@bp.route("/suppliers/<int:sid>/pay", methods=["GET", "POST"])
@requires("treasury.transfer")
def pay(sid):
    supplier = db.get_or_404(Supplier, sid)
    open_invoices = [
        i for i in db.session.scalars(
            db.select(PurchaseInvoice).filter_by(supplier_id=sid, payment_type="credit"))
        if services.invoice_outstanding(i) > 0]
    sources = [(aid, lbl) for aid, lbl, cur in _pay_sources()
               if cur == supplier.currency_code]
    if request.method == "POST":
        pay_from = request.form.get("pay_from_account_id", type=int)
        amount = request.form.get("amount") or 0
        allocations = []
        for i in open_invoices:
            a = request.form.get(f"alloc_{i.id}")
            if a and to_decimal(a) > 0:
                allocations.append((i, a))
        from app.core.models import Branch
        branch = db.session.scalar(db.select(Branch))
        services.create_supplier_payment(
            supplier=supplier, pay_from_account_id=pay_from, amount=amount,
            allocations=allocations, branch_id=branch.id if branch else None,
            user_id=current_user.id)
        db.session.commit()
        flash("تم تسجيل السداد.", "success")
        return redirect(url_for("purchasing.supplier_view", sid=sid))
    return render_template("purchasing/pay.html", supplier=supplier,
                           invoices=open_invoices, sources=sources,
                           outstanding=services.invoice_outstanding)


# --- aging & due ----------------------------------------------------------
@bp.route("/aging")
@requires("purchase.invoice.create")
def aging():
    return render_template("purchasing/aging.html", rows=services.aging(),
                           due=services.due_soon())


# --- purchase orders (spec PUR-01) ----------------------------------------
@bp.route("/orders")
@requires("purchase.invoice.create")
def orders():
    from app.purchasing.models import PurchaseOrder
    items = db.session.scalars(
        db.select(PurchaseOrder).order_by(PurchaseOrder.id.desc()).limit(100)).all()
    return render_template("purchasing/orders.html", orders=items)


@bp.route("/orders/new", methods=["GET", "POST"])
@requires("purchase.invoice.create")
def order_new():
    from app.inventory.models import Product, Warehouse
    products = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    warehouses = db.session.scalars(db.select(Warehouse)).all()
    suppliers_ = db.session.scalars(db.select(Supplier).order_by(Supplier.name_ar)).all()
    if request.method == "POST":
        warehouse = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
        supplier = db.session.get(Supplier, request.form.get("supplier_id", type=int))
        pids = request.form.getlist("product_id")
        qtys = request.form.getlist("qty")
        costs = request.form.getlist("cost")
        lines = []
        for i, pid in enumerate(pids):
            if pid and qtys[i] and to_decimal(qtys[i]) > 0:
                lines.append((db.get_or_404(Product, int(pid)), qtys[i], costs[i]))
        if not lines:
            flash("أضف أصنافًا.", "error"); return redirect(request.url)
        order = services.create_order(supplier=supplier, warehouse=warehouse,
                                      lines=lines, user_id=current_user.id)
        db.session.commit()
        return redirect(url_for("purchasing.order_view", oid=order.id))
    return render_template("purchasing/order_form.html", products=products,
                           warehouses=warehouses, suppliers=suppliers_)


@bp.route("/orders/<int:oid>")
@requires("purchase.invoice.create")
def order_view(oid):
    from app.purchasing.models import PurchaseOrder
    order = db.get_or_404(PurchaseOrder, oid)
    return render_template("purchasing/order_view.html", order=order,
                           sources=_pay_sources())


@bp.route("/orders/<int:oid>/<action>", methods=["POST"])
@requires("purchase.invoice.create")
def order_action(oid, action):
    from app.purchasing.models import PurchaseOrder
    order = db.get_or_404(PurchaseOrder, oid)
    try:
        if action == "approve":
            services.approve_order(order, user_id=current_user.id)
        elif action == "cancel":
            services.cancel_order(order, user_id=current_user.id)
        elif action == "convert":
            inv = services.convert_order_to_invoice(
                order, payment_type=request.form.get("payment_type", "credit"),
                pay_from_account_id=request.form.get("pay_from_account_id", type=int),
                user_id=current_user.id)
            db.session.commit()
            flash(f"تم تحويل الأمر إلى فاتورة {inv.number}.", "success")
            return redirect(url_for("purchasing.invoice_view", inv_id=inv.id))
        db.session.commit()
        flash("تم.", "success")
    except (ValueError, services.InsufficientCustodyError) as e:
        db.session.rollback(); flash(str(e), "error")
    return redirect(url_for("purchasing.order_view", oid=oid))


@bp.route("/invoices/<int:inv_id>/return", methods=["POST"])
@requires("purchase.invoice.post")
def invoice_return(inv_id):
    invoice = db.get_or_404(PurchaseInvoice, inv_id)
    try:
        services.create_purchase_return(invoice=invoice, user_id=current_user.id)
        db.session.commit()
        flash("تم تسجيل مرتجع المشتريات.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذّر المرتجع: {e}", "error")
    return redirect(url_for("purchasing.invoice_view", inv_id=inv_id))
