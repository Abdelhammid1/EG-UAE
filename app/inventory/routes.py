"""Inventory + mini-POS screens (Phase 2.5 slice)."""
from __future__ import annotations

from decimal import Decimal

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

from app.core.permissions import requires
from app.extensions import db
from app.inventory import services
from app.inventory.models import Product, Sale, Warehouse
from app.treasury.models import Treasury

bp = Blueprint("inventory", __name__)


@bp.before_request
@login_required
def _guard():
    pass


# --- products -------------------------------------------------------------
@bp.route("/products")
@requires("accounting.journal.create")
def products():
    items = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    return render_template("inventory/products.html", products=items)


@bp.route("/products/new", methods=["GET", "POST"])
@requires("accounting.journal.create")
def product_new():
    if request.method == "POST":
        from app.core import uploads
        p = Product(
            name_ar=request.form["name_ar"].strip(),
            barcode=request.form.get("barcode", "").strip() or None,
            category=request.form.get("category", "").strip() or None,
            unit=request.form.get("unit", "قطعة").strip() or "قطعة",
            default_price=request.form.get("default_price") or 0,
            wholesale_price=request.form.get("wholesale_price") or 0,
            min_price=request.form.get("min_price") or 0,
            valuation_method=request.form.get("valuation_method", "FIFO"),
            reorder_level=request.form.get("reorder_level") or 0,
            track_serial=request.form.get("track_serial") == "1",
            weight=request.form.get("weight") or 0,
            created_by_id=current_user.id,
        )
        try:
            p.image_path = uploads.save_image(request.files.get("image"), "products")
        except ValueError as e:
            flash(str(e), "error")
            return render_template("inventory/product_form.html", p=None)
        db.session.add(p)
        db.session.commit()
        flash("تم إنشاء المنتج.", "success")
        return redirect(url_for("inventory.products"))
    return render_template("inventory/product_form.html", p=None)


@bp.route("/products/<int:pid>/edit", methods=["GET", "POST"])
@requires("accounting.journal.create")
def product_edit(pid):
    p = db.get_or_404(Product, pid)
    if request.method == "POST":
        from app.core import uploads
        p.name_ar = request.form["name_ar"].strip()
        p.barcode = request.form.get("barcode", "").strip() or None
        p.category = request.form.get("category", "").strip() or None
        p.unit = request.form.get("unit", "قطعة").strip() or "قطعة"
        p.default_price = request.form.get("default_price") or 0
        p.wholesale_price = request.form.get("wholesale_price") or 0
        p.min_price = request.form.get("min_price") or 0
        p.valuation_method = request.form.get("valuation_method", "FIFO")
        p.reorder_level = request.form.get("reorder_level") or 0
        p.track_serial = request.form.get("track_serial") == "1"
        p.weight = request.form.get("weight") or 0
        try:
            new_img = uploads.save_image(request.files.get("image"), "products")
        except ValueError as e:
            flash(str(e), "error")
            return render_template("inventory/product_form.html", p=p)
        if new_img:
            uploads.delete_image(p.image_path)
            p.image_path = new_img
        elif request.form.get("remove_image") == "1":
            uploads.delete_image(p.image_path)
            p.image_path = None
        db.session.commit()
        flash("تم تحديث المنتج.", "success")
        return redirect(url_for("inventory.products"))
    return render_template("inventory/product_form.html", p=p)


@bp.route("/products/<int:pid>/label")
@requires("accounting.journal.create")
def product_label(pid):
    """Printable barcode label(s) for a product (spec INV-01.7)."""
    p = db.get_or_404(Product, pid)
    count = min(max(request.args.get("count", 1, type=int) or 1, 1), 60)
    svg = services.barcode_svg(p.barcode) if p.barcode else None
    return render_template("inventory/labels.html", products=[(p, svg)],
                           count=count)


@bp.route("/labels")
@requires("accounting.journal.create")
def labels():
    """Bulk barcode-label sheet for all products that have a barcode."""
    count = min(max(request.args.get("count", 1, type=int) or 1, 1), 10)
    items = []
    for p in db.session.scalars(db.select(Product).order_by(Product.name_ar)).all():
        if p.barcode:
            items.append((p, services.barcode_svg(p.barcode)))
    return render_template("inventory/labels.html", products=items, count=count)


# --- warehouses + receiving ----------------------------------------------
@bp.route("/warehouses")
@requires("accounting.journal.create")
def warehouses():
    whs = db.session.scalars(db.select(Warehouse)).all()
    rows = []
    for w in whs:
        lines = []
        for p in db.session.scalars(db.select(Product)).all():
            q = services.qty_on_hand(p.id, w.id)
            if q:
                lines.append({"product": p, "qty": q})
        rows.append({"wh": w, "stock": lines})
    return render_template("inventory/warehouses.html", rows=rows)


@bp.route("/receive", methods=["GET", "POST"])
@requires("accounting.journal.create")
def receive():
    products_ = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    whs = db.session.scalars(db.select(Warehouse)).all()
    if request.method == "POST":
        p = db.get_or_404(Product, request.form.get("product_id", type=int))
        w = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
        serials = None
        if p.track_serial:
            raw = request.form.get("serials", "")
            serials = [s.strip() for s in raw.replace(",", "\n").splitlines() if s.strip()]
        try:
            services.receive_stock(
                product=p, warehouse=w, qty=request.form["qty"],
                unit_cost=request.form["unit_cost"], user_id=current_user.id,
                source="receive", serials=serials,
            )
            db.session.commit()
            flash("تم استلام المخزون وقيده.", "success")
            return redirect(url_for("inventory.warehouses"))
        except services.SerialError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("inventory/receive.html", products=products_, warehouses=whs)


# --- mini POS -------------------------------------------------------------
@bp.route("/pos")
@requires("pos.sell")
def pos():
    from app.sales.services import open_shift_for
    from app.sales.models import Customer
    from app.treasury.models import BankAccount
    whs = db.session.scalars(db.select(Warehouse)).all()
    drawers = db.session.scalars(
        db.select(Treasury).filter_by(type="drawer", is_active=True)).all()
    default_wh = whs[0] if whs else None
    drawer = drawers[0] if drawers else None
    shift = open_shift_for(drawer.id) if drawer else None

    products_ = []
    if default_wh:
        for p in db.session.scalars(db.select(Product).order_by(Product.name_ar)).all():
            products_.append({
                "id": p.id, "name": p.name_ar, "price": float(p.default_price),
                "wprice": float(p.wholesale_price or 0),
                "min": float(p.min_price), "barcode": p.barcode or "",
                "stock": float(services.qty_on_hand(p.id, default_wh.id)),
                "serial": bool(p.track_serial),
                "img": (url_for("static", filename=p.image_path)
                        if p.image_path else ""),
            })
    from app.sales.models import Funder, HeldSale
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name_ar)).all()
    # map customer id -> type so the POS can switch to wholesale pricing (SAL-02)
    cust_types = {c.id: c.type for c in customers}
    banks = db.session.scalars(db.select(BankAccount).filter_by(is_active=True)).all()
    funders = db.session.scalars(db.select(Funder).order_by(Funder.name_ar)).all()
    held = db.session.scalars(
        db.select(HeldSale).order_by(HeldSale.id.desc())).all()
    # a cart to prefill when recalling a held sale
    recall = None
    rid = request.args.get("recall", type=int)
    if rid:
        h = db.session.get(HeldSale, rid)
        if h:
            import json
            try:
                recall = json.loads(h.payload)
                recall["held_id"] = h.id
            except (ValueError, TypeError):
                recall = None
    return render_template(
        "inventory/pos.html", warehouses=whs, drawer=drawer, shift=shift,
        products=products_, customers=customers, banks=banks, funders=funders,
        cust_types=cust_types, held=held, recall=recall,
        can_below_min=current_user.has_permission("sale.below_min"),
        can_shift=current_user.has_permission("shift.manage"),
    )


@bp.route("/pos/sell", methods=["POST"])
@requires("pos.sell")
def pos_sell():
    from app.sales import services as sales_services
    from app.sales.models import Customer, Funder
    from app.treasury.models import BankAccount
    from datetime import datetime
    warehouse = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
    drawer = db.get_or_404(Treasury, request.form.get("treasury_id", type=int))
    payment_type = request.form.get("payment_type", "cash")
    customer = db.session.get(Customer, request.form.get("customer_id", type=int))
    bank = db.session.get(BankAccount, request.form.get("bank_account_id", type=int))
    funder = db.session.get(Funder, request.form.get("funder_id", type=int))
    shift = sales_services.open_shift_for(drawer.id)

    installment = None
    if payment_type == "installment":
        try:
            first_due = datetime.strptime(request.form.get("inst_first_due", ""),
                                          "%Y-%m-%d").date()
        except ValueError:
            from datetime import date as _d
            first_due = _d.today()
        installment = {
            "count": request.form.get("inst_count", type=int) or 1,
            "first_due": first_due,
            "down_payment": request.form.get("inst_down") or 0,
            "down_treasury": drawer,
        }

    pids = request.form.getlist("product_id")
    qtys = request.form.getlist("qty")
    prices = request.form.getlist("price")
    serial_lists = request.form.getlist("serials")
    allow_below = (request.form.get("allow_below_min") == "1"
                   and current_user.has_permission("sale.below_min"))
    allow_over_limit = (request.form.get("allow_over_limit") == "1"
                        and current_user.has_permission("sale.below_min"))

    lines = []
    for i, pid in enumerate(pids):
        if not pid or not qtys[i] or float(qtys[i] or 0) <= 0:
            continue
        product = db.get_or_404(Product, int(pid))
        if product.track_serial:
            raw = serial_lists[i] if i < len(serial_lists) else ""
            serials = [s.strip() for s in raw.replace(",", "\n").splitlines() if s.strip()]
            lines.append((product, qtys[i], prices[i], serials))
        else:
            lines.append((product, qtys[i], prices[i]))

    # split payment: parallel cash/card amounts (spec POS-03)
    split = None
    if payment_type == "split":
        split = []
        cash_amt = request.form.get("split_cash")
        card_amt = request.form.get("split_card")
        if cash_amt and float(cash_amt or 0) > 0:
            split.append(("cash", cash_amt, drawer.account_id))
        if card_amt and float(card_amt or 0) > 0 and bank:
            split.append(("card", card_amt, bank.account_id))

    try:
        sale = sales_services.make_sale(
            warehouse=warehouse, lines=lines, payment_type=payment_type,
            treasury=drawer, bank_account=bank, customer=customer, funder=funder,
            shift=shift, installment=installment, payments=split,
            discount=request.form.get("discount") or 0, user_id=current_user.id,
            allow_below_min=allow_below, allow_over_limit=allow_over_limit)
        # if this cart came from a held (parked) sale, clear it now
        held_id = request.form.get("held_id", type=int)
        if held_id:
            from app.sales.models import HeldSale
            h = db.session.get(HeldSale, held_id)
            if h:
                db.session.delete(h)
        db.session.commit()
        flash(f"تم البيع {sale.number}.", "success")
        return redirect(url_for("inventory.sale_view", sale_id=sale.id))
    except (services.MinPriceViolation, services.OutOfStockError,
            services.EmptySaleError, services.SerialError,
            sales_services.NoOpenShiftError, sales_services.CreditLimitError,
            ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("inventory.pos"))


@bp.route("/pos/hold", methods=["POST"])
@requires("pos.sell")
def pos_hold():
    """Park the current cart so it can be recalled later (spec POS-02.7)."""
    import json
    from app.sales.models import HeldSale
    payload = request.form.get("payload", "")
    label = (request.form.get("label") or "").strip() or "فاتورة معلّقة"
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        flash("لا توجد سلة لتعليقها.", "error")
        return redirect(url_for("inventory.pos"))
    if not data.get("cart"):
        flash("السلة فارغة — لا يوجد ما يُعلَّق.", "error")
        return redirect(url_for("inventory.pos"))
    branch_id = None
    wh = db.session.get(Warehouse, data.get("warehouseId"))
    if wh:
        branch_id = wh.branch_id
    held = HeldSale(label=label[:80], branch_id=branch_id,
                    cashier_id=current_user.id, payload=payload)
    db.session.add(held)
    db.session.commit()
    flash(f"تم تعليق الفاتورة: {label}.", "success")
    return redirect(url_for("inventory.pos"))


@bp.route("/pos/held/<int:hid>/delete", methods=["POST"])
@requires("pos.sell")
def pos_held_delete(hid):
    from app.sales.models import HeldSale
    held = db.session.get(HeldSale, hid)
    if held:
        db.session.delete(held)
        db.session.commit()
        flash("تم حذف الفاتورة المعلّقة.", "info")
    return redirect(url_for("inventory.pos"))


@bp.route("/pos/exchange", methods=["GET"])
@requires("pos.sell")
def exchange():
    """Exchange screen: find a sale, return some lines, sell replacements."""
    from app.sales.models import Customer
    from app.sales import services as ss
    number = (request.args.get("number") or "").strip()
    original = None
    if number:
        original = db.session.scalar(db.select(Sale).filter_by(number=number))
        if original is None:
            flash("لم يتم العثور على فاتورة بهذا الرقم.", "error")
    whs = db.session.scalars(db.select(Warehouse)).all()
    drawers = db.session.scalars(
        db.select(Treasury).filter_by(type="drawer", is_active=True)).all()
    drawer = drawers[0] if drawers else None
    shift = ss.open_shift_for(drawer.id) if drawer else None
    products_ = []
    default_wh = original.warehouse if original else (whs[0] if whs else None)
    if default_wh:
        for p in db.session.scalars(db.select(Product).order_by(Product.name_ar)).all():
            products_.append({
                "id": p.id, "name": p.name_ar, "price": float(p.default_price),
                "min": float(p.min_price), "barcode": p.barcode or "",
                "stock": float(services.qty_on_hand(p.id, default_wh.id)),
                "serial": bool(p.track_serial),
            })
    # remaining returnable qty per line
    lines = []
    if original:
        for l in original.lines:
            remaining = float(l.qty) - float(ss.returned_qty(l))
            lines.append({"line": l, "remaining": remaining})
    from app.treasury.models import BankAccount
    banks = db.session.scalars(db.select(BankAccount).filter_by(is_active=True)).all()
    return render_template(
        "inventory/exchange.html", original=original, lines=lines, number=number,
        products=products_, drawer=drawer, shift=shift, warehouse=default_wh,
        banks=banks, can_below_min=current_user.has_permission("sale.below_min"))


@bp.route("/pos/exchange", methods=["POST"])
@requires("pos.sell")
def exchange_do():
    from app.sales import services as ss
    from app.treasury.models import BankAccount
    original = db.get_or_404(Sale, request.form.get("original_id", type=int))
    warehouse = original.warehouse
    drawer = db.session.get(Treasury, request.form.get("treasury_id", type=int))
    shift = ss.open_shift_for(drawer.id) if drawer else None
    payment_type = request.form.get("payment_type", "cash")
    bank = db.session.get(BankAccount, request.form.get("bank_account_id", type=int))

    # returned quantities
    return_qtys = {}
    for l in original.lines:
        v = request.form.get(f"ret_{l.id}")
        if v and float(v or 0) > 0:
            return_qtys[l.id] = v

    # new items
    pids = request.form.getlist("product_id")
    qtys = request.form.getlist("qty")
    prices = request.form.getlist("price")
    new_lines = []
    for i, pid in enumerate(pids):
        if not pid or not qtys[i] or float(qtys[i] or 0) <= 0:
            continue
        product = db.get_or_404(Product, int(pid))
        new_lines.append((product, qtys[i], prices[i]))

    allow_below = (request.form.get("allow_below_min") == "1"
                   and current_user.has_permission("sale.below_min"))
    try:
        result = ss.make_exchange(
            original_sale=original, return_line_qtys=return_qtys, new_lines=new_lines,
            warehouse=warehouse, shift=shift, treasury=drawer, bank_account=bank,
            payment_type=payment_type, user_id=current_user.id,
            allow_below_min=allow_below)
        db.session.commit()
        diff = result["difference"]
        msg = (f"تم الاستبدال. المرتجع {result['return'].number} والبيع "
               f"{result['sale'].number}. ")
        if diff > 0:
            msg += f"يُحصَّل من العميل: {diff}."
        elif diff < 0:
            msg += f"يُرَدّ للعميل: {-diff}."
        else:
            msg += "استبدال متعادل بدون فرق."
        flash(msg, "success")
        return redirect(url_for("inventory.sale_view", sale_id=result["sale"].id))
    except (services.MinPriceViolation, services.OutOfStockError,
            services.EmptySaleError, services.SerialError,
            ss.NoOpenShiftError, ss.AlreadyReturnedError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("inventory.exchange", number=original.number))


@bp.route("/sales/<int:sale_id>")
@requires("pos.sell")
def sale_view(sale_id):
    sale = db.get_or_404(Sale, sale_id)
    return render_template("inventory/sale_view.html", sale=sale)


@bp.route("/sales/<int:sale_id>/receipt")
@requires("pos.sell")
def sale_receipt(sale_id):
    sale = db.get_or_404(Sale, sale_id)
    reprint = request.args.get("reprint") == "1"
    if reprint:
        from app.core import audit
        audit.record(action="sale.reprint", entity="sale", entity_id=sale.id)
        db.session.commit()
    return render_template("inventory/receipt.html", sale=sale, reprint=reprint)


@bp.route("/sales/<int:sale_id>/receipt.pdf")
@requires("pos.sell")
def sale_receipt_pdf(sale_id):
    import io
    from flask import send_file
    from app.core import settings
    from app.core.pdf import build_pdf
    sale = db.get_or_404(Sale, sale_id)
    try:
        company = settings.get("company.name") or "منصتي"
    except Exception:  # noqa: BLE001
        company = "منصتي"
    methods = {"cash": "نقدي", "card": "بطاقة", "credit": "آجل", "funder": "تمويل",
               "installment": "تقسيط", "split": "مقسّم"}
    rows = [[l.product.name_ar, f"{l.qty:g}", f"{l.unit_price:,.2f}",
             f"{l.line_total:,.2f}"] for l in sale.lines]
    blocks = [
        {"type": "title", "text": company},
        {"type": "paragraph", "text": "فاتورة بيع"},
        {"type": "meta", "pairs": [
            ("رقم الفاتورة", sale.number), ("التاريخ", sale.date.isoformat()),
            ("طريقة الدفع", methods.get(sale.payment_type, sale.payment_type)),
            ("العميل", sale.customer.name_ar if sale.customer else "نقدي")]},
        {"type": "spacer", "height": 2},
        {"type": "table", "headers": ["الصنف", "كمية", "سعر", "إجمالي"],
         "rows": rows, "aligns": ["R", "C", "C", "C"]},
        {"type": "spacer", "height": 2},
        {"type": "meta", "pairs": [
            ("الإجمالي قبل الضريبة", f"{sale.subtotal:,.2f} {sale.currency_code}"),
            ("الضريبة", f"{sale.tax_amount:,.2f} {sale.currency_code}"),
            ("الإجمالي المستحق", f"{sale.total:,.2f} {sale.currency_code}")]},
        {"type": "spacer", "height": 3},
        {"type": "paragraph", "text": "شكرًا لتعاملكم معنا 🌟"},
    ]
    pdf = build_pdf(blocks, title=f"receipt-{sale.number}")
    return send_file(io.BytesIO(pdf), download_name=f"receipt-{sale.number}.pdf",
                     mimetype="application/pdf")


# --- reorder alerts (spec §7.5) -------------------------------------------
@bp.route("/reorder")
@requires("accounting.journal.create")
def reorder():
    alerts = services.reorder_alerts()
    return render_template("inventory/reorder.html", alerts=alerts)


# --- stocktake (spec §7 الجرد) --------------------------------------------
@bp.route("/stocktake")
@requires("accounting.journal.create")
def stocktakes():
    from app.inventory.models import Stocktake
    items = db.session.scalars(
        db.select(Stocktake).order_by(Stocktake.id.desc())).all()
    whs = db.session.scalars(db.select(Warehouse)).all()
    return render_template("inventory/stocktakes.html", items=items, warehouses=whs)


@bp.route("/stocktake/open", methods=["POST"])
@requires("accounting.journal.create")
def stocktake_open():
    w = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
    st = services.open_stocktake(warehouse=w, user_id=current_user.id)
    db.session.commit()
    return redirect(url_for("inventory.stocktake_view", st_id=st.id))


@bp.route("/stocktake/<int:st_id>", methods=["GET", "POST"])
@requires("accounting.journal.create")
def stocktake_view(st_id):
    from app.inventory.models import Stocktake
    st = db.get_or_404(Stocktake, st_id)
    if request.method == "POST":
        action = request.form.get("action")
        counts = {}
        for line in st.lines:
            v = request.form.get(f"count_{line.id}")
            if v not in (None, ""):
                counts[line.product_id] = v
        try:
            services.save_counts(st, counts)
            if action == "approve":
                approver = current_user.id if current_user.has_role("owner") else None
                services.approve_stocktake(st, user_id=current_user.id,
                                           approver_id=approver)
                flash("تم اعتماد الجرد وتسويته.", "success")
            else:
                flash("تم حفظ الكميات.", "success")
            db.session.commit()
        except services.StocktakeError as e:
            db.session.rollback()
            flash(str(e) + (" (يلزم أن يعتمده المالك)." if "المالك" in str(e) else ""),
                  "error")
        return redirect(url_for("inventory.stocktake_view", st_id=st_id))
    return render_template("inventory/stocktake_view.html", st=st)


@bp.route("/damage", methods=["GET", "POST"])
@requires("accounting.journal.create")
def damage():
    products_ = db.session.scalars(db.select(Product).order_by(Product.name_ar)).all()
    whs = db.session.scalars(db.select(Warehouse)).all()
    if request.method == "POST":
        p = db.get_or_404(Product, request.form.get("product_id", type=int))
        w = db.get_or_404(Warehouse, request.form.get("warehouse_id", type=int))
        try:
            services.write_off(product=p, warehouse=w, qty=request.form["qty"],
                               reason=request.form.get("reason"), user_id=current_user.id)
            db.session.commit()
            flash("تم تسجيل التالف/الفقد وقيده.", "success")
            return redirect(url_for("inventory.warehouses"))
        except services.OutOfStockError as e:
            db.session.rollback(); flash(str(e), "error")
    return render_template("inventory/damage.html", products=products_, warehouses=whs)
