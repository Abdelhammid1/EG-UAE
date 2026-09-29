"""Customers, shifts, collections, returns (spec §10, §11)."""
from __future__ import annotations

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core.permissions import requires
from app.extensions import db
from app.sales import services
from app.sales.models import Collection, Customer, SalesReturn, Shift
from app.treasury.models import BankAccount, Treasury

bp = Blueprint("sales", __name__)


@bp.before_request
@login_required
def _guard():
    pass


# --- customers ------------------------------------------------------------
@bp.route("/customers")
@requires("pos.sell")
def customers():
    items = db.session.scalars(db.select(Customer).order_by(Customer.name_ar)).all()
    bal = {c.id: services.ar_balance(c) for c in items}
    return render_template("sales/customers.html", customers=items, bal=bal)


@bp.route("/customers/new", methods=["GET", "POST"])
@requires("pos.sell")
def customer_new():
    from app.core.models import Currency, Branch
    if request.method == "POST":
        branch = db.session.scalar(db.select(Branch))
        services.create_customer(
            name_ar=request.form["name_ar"].strip(),
            phone=request.form.get("phone", ""),
            type=request.form.get("type", "retail"),
            credit_limit=request.form.get("credit_limit") or 0,
            opening_balance=request.form.get("opening_balance") or 0,
            currency_code=request.form.get("currency_code", "AED"),
            branch_id=branch.id if branch else None, user_id=current_user.id)
        db.session.commit()
        flash("تم إنشاء العميل.", "success")
        return redirect(url_for("sales.customers"))
    currencies = db.session.scalars(db.select(Currency)).all()
    return render_template("sales/customer_form.html", currencies=currencies)


@bp.route("/customers/<int:cid>")
@requires("pos.sell")
def customer_view(cid):
    c = db.get_or_404(Customer, cid)
    from app.treasury.services import statement
    rows = statement(c.account_id)
    return render_template("sales/customer_view.html", customer=c, rows=rows,
                           balance=services.ar_balance(c))


@bp.route("/customers/<int:cid>/collect", methods=["GET", "POST"])
@requires("treasury.transfer")
def collect(cid):
    c = db.get_or_404(Customer, cid)
    sources = []
    for t in db.session.scalars(db.select(Treasury).filter_by(is_active=True)):
        if t.currency_code == c.currency_code:
            sources.append((t.account_id, f"خزينة: {t.name_ar}"))
    for b in db.session.scalars(db.select(BankAccount).filter_by(is_active=True)):
        if b.currency_code == c.currency_code:
            sources.append((b.account_id, f"بنك: {b.name_ar}"))
    if request.method == "POST":
        from app.core.models import Branch
        branch = db.session.scalar(db.select(Branch))
        services.collect(customer=c, to_account_id=request.form.get("to_account_id", type=int),
                         amount=request.form["amount"],
                         branch_id=branch.id if branch else None, user_id=current_user.id)
        db.session.commit()
        flash("تم التحصيل.", "success")
        return redirect(url_for("sales.customer_view", cid=cid))
    return render_template("sales/collect.html", customer=c, sources=sources)


# --- shifts (spec §11) ----------------------------------------------------
@bp.route("/shift/open", methods=["POST"])
@requires("shift.manage")
def shift_open():
    drawer = db.get_or_404(Treasury, request.form.get("drawer_id", type=int))
    try:
        services.open_shift(cashier_id=current_user.id, drawer=drawer,
                            opening_cash=request.form.get("opening_cash") or 0,
                            user_id=current_user.id)
        db.session.commit()
        flash("تم فتح الوردية.", "success")
    except services.ShiftError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("inventory.pos"))


@bp.route("/shift/<int:sid>/close", methods=["GET", "POST"])
@requires("shift.manage")
def shift_close(sid):
    shift = db.get_or_404(Shift, sid)
    if request.method == "POST":
        try:
            services.close_shift(shift=shift, counted_cash=request.form["counted_cash"],
                                 user_id=current_user.id)
            db.session.commit()
            flash("تم إغلاق الوردية.", "success")
            return redirect(url_for("sales.shift_view", sid=sid))
        except services.ShiftError as e:
            db.session.rollback()
            flash(str(e), "error")
    expected = services.shift_expected_cash(shift)
    return render_template("sales/shift_close.html", shift=shift, expected=expected,
                           cash_sales=services.shift_cash_sales(shift))


@bp.route("/shift/<int:sid>")
@requires("shift.manage")
def shift_view(sid):
    shift = db.get_or_404(Shift, sid)
    return render_template("sales/shift_view.html", shift=shift,
                           cash_sales=services.shift_cash_sales(shift))


# --- returns (spec §10.7) -------------------------------------------------
@bp.route("/return/<int:sale_id>", methods=["POST"])
@requires("sales.return.create")
def do_return(sale_id):
    from app.inventory.models import Sale
    sale = db.get_or_404(Sale, sale_id)
    # separation of duties: creator cannot approve own return if approval needed
    try:
        ret = services.sales_return(sale=sale, user_id=current_user.id)
        db.session.commit()
        flash(f"تم إنشاء مرتجع {ret.number}.", "success")
    except services.AlreadyReturnedError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("inventory.sale_view", sale_id=sale_id))


# --- BNPL funders (spec §10) ----------------------------------------------
@bp.route("/funders")
@requires("accounting.journal.create")
def funders():
    from app.sales.models import Funder
    items = db.session.scalars(db.select(Funder).order_by(Funder.name_ar)).all()
    bal = {f.id: services.funder_open_receivable(f) for f in items}
    return render_template("sales/funders.html", funders=items, bal=bal)


@bp.route("/funders/new", methods=["GET", "POST"])
@requires("accounting.journal.create")
def funder_new():
    from app.core.models import Branch, Currency
    from app.treasury.models import BankAccount
    if request.method == "POST":
        branch = db.session.scalar(db.select(Branch))
        bank = db.session.get(BankAccount, request.form.get("bank_id", type=int))
        services.create_funder(
            name_ar=request.form["name_ar"].strip(),
            branch_id=branch.id if branch else None,
            currency_code=request.form.get("currency_code", "AED"),
            commission_pct=request.form.get("commission_pct") or 0,
            fixed_fee=request.form.get("fixed_fee") or 0,
            settlement_bank_account_id=bank.account_id if bank else None,
            user_id=current_user.id)
        db.session.commit()
        flash("تم إنشاء جهة التمويل.", "success")
        return redirect(url_for("sales.funders"))
    currencies = db.session.scalars(db.select(Currency)).all()
    banks = db.session.scalars(db.select(BankAccount).filter_by(is_active=True)).all()
    return render_template("sales/funder_form.html", currencies=currencies, banks=banks)


@bp.route("/funders/<int:fid>/settle", methods=["GET", "POST"])
@requires("accounting.journal.create")
def funder_settle(fid):
    from app.sales.models import Funder
    from app.inventory.models import Sale
    from app.treasury.models import BankAccount
    funder = db.get_or_404(Funder, fid)
    pending = db.session.scalars(
        db.select(Sale).filter_by(funder_id=fid, funder_settled=False)).all()
    banks = db.session.scalars(db.select(BankAccount).filter_by(is_active=True)).all()
    if request.method == "POST":
        ids = request.form.getlist("sale_id")
        sales = [db.get_or_404(Sale, int(i)) for i in ids]
        bank = db.get_or_404(BankAccount, request.form.get("bank_id", type=int))
        services.settle_funder(funder=funder, sales=sales,
                               bank_account_id=bank.account_id,
                               received_amount=request.form["received_amount"],
                               user_id=current_user.id)
        db.session.commit()
        flash("تمت التسوية.", "success")
        return redirect(url_for("sales.funders"))
    return render_template("sales/funder_settle.html", funder=funder,
                           pending=pending, banks=banks,
                           total=sum((s.total for s in pending), 0))


@bp.route("/installments/overdue")
@requires("pos.sell")
def installments_overdue():
    dues = services.overdue_installments()
    return render_template("sales/installments.html", dues=dues)


# --- quotes (spec SAL-08) -------------------------------------------------
@bp.route("/quotes")
@requires("pos.sell")
def quotes():
    from app.sales.models import Quote
    items = db.session.scalars(
        db.select(Quote).order_by(Quote.id.desc()).limit(100)).all()
    return render_template("sales/quotes.html", quotes=items)


@bp.route("/quotes/new", methods=["GET", "POST"])
@requires("pos.sell")
def quote_new():
    from app.inventory.models import Product, Warehouse
    products = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    warehouses = db.session.scalars(db.select(Warehouse)).all()
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name_ar)).all()
    if request.method == "POST":
        from datetime import datetime
        warehouse = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
        customer = db.session.get(Customer, request.form.get("customer_id", type=int))
        valid = request.form.get("valid_until")
        valid_until = None
        if valid:
            try:
                valid_until = datetime.strptime(valid, "%Y-%m-%d").date()
            except ValueError:
                pass
        pids = request.form.getlist("product_id")
        qtys = request.form.getlist("qty")
        prices = request.form.getlist("price")
        lines = []
        for i, pid in enumerate(pids):
            if pid and qtys[i] and float(qtys[i] or 0) > 0:
                lines.append((db.get_or_404(Product, int(pid)), qtys[i], prices[i]))
        if not lines:
            flash("أضف أصنافًا.", "error"); return redirect(request.url)
        q = services.create_quote(customer=customer, warehouse=warehouse, lines=lines,
                                  valid_until=valid_until, user_id=current_user.id)
        db.session.commit()
        return redirect(url_for("sales.quote_view", qid=q.id))
    return render_template("sales/quote_form.html", products=products,
                           warehouses=warehouses, customers=customers)


@bp.route("/quotes/<int:qid>")
@requires("pos.sell")
def quote_view(qid):
    from app.sales.models import Quote
    q = db.get_or_404(Quote, qid)
    return render_template("sales/quote_view.html", quote=q)


@bp.route("/quotes/<int:qid>/convert", methods=["POST"])
@requires("pos.sell")
def quote_convert(qid):
    from app.sales.models import Quote
    from app.inventory.services import MinPriceViolation, OutOfStockError
    q = db.get_or_404(Quote, qid)
    try:
        sale = services.convert_quote_to_sale(q, payment_type="credit",
                                              user_id=current_user.id)
        db.session.commit()
        flash(f"تم تحويل العرض إلى فاتورة {sale.number}.", "success")
        return redirect(url_for("inventory.sale_view", sale_id=sale.id))
    except (ValueError, MinPriceViolation, OutOfStockError) as e:
        db.session.rollback(); flash(str(e), "error")
        return redirect(url_for("sales.quote_view", qid=qid))
