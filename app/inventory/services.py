"""Inventory + sales services (Phase 3).

Valuation is per batch (spec §7.3): FIFO batches consume oldest-first at their
own cost; weighted-average batches consume at the pool's average. Costs are
kept in the warehouse's currency and the book currency (§5.2, §7.5), so COGS
posts in book while warehouse reports show native. Serial (IMEI) products track
each unit through receipt and sale, never selling one twice (§16.14, §16.15).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import current_app

from app.accounting import posting
from app.core import audit, fx
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.core.numbering import next_number
from app.extensions import db
from app.inventory.models import (
    Product,
    ProductSerial,
    Sale,
    SaleLine,
    Stocktake,
    StocktakeLine,
    StockBatch,
    StockMovement,
    Warehouse,
)

BOOK = "AED"


class OutOfStockError(Exception):
    pass


class MinPriceViolation(Exception):
    pass


class EmptySaleError(Exception):
    pass


class SerialError(Exception):
    pass


class StocktakeError(Exception):
    pass


# --- helpers --------------------------------------------------------------
def warehouse_currency(warehouse: Warehouse) -> str:
    """A warehouse reports in its country's currency (spec §7.5)."""
    try:
        return warehouse.branch.country.currency_code
    except Exception:
        return current_app.config.get("BOOK_CURRENCY", BOOK)


def qty_on_hand(product_id: int, warehouse_id: int) -> Decimal:
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(StockBatch.qty_remaining), 0))
        .filter_by(product_id=product_id, warehouse_id=warehouse_id)
    )
    return quantize_amount(total)


def stock_value_book(product_id: int, warehouse_id: int) -> Decimal:
    """Book value of remaining stock (sum qty_remaining * unit_cost_book)."""
    batches = db.session.scalars(
        db.select(StockBatch).filter_by(product_id=product_id,
                                        warehouse_id=warehouse_id)
    ).all()
    return quantize_amount(sum(
        (b.qty_remaining * b.unit_cost_book for b in batches), Decimal("0")))


def avg_unit_cost_book(product_id: int, warehouse_id: int) -> Decimal:
    qty = qty_on_hand(product_id, warehouse_id)
    if qty == 0:
        return Decimal("0")
    return quantize_amount(stock_value_book(product_id, warehouse_id) / qty)


# --- receive --------------------------------------------------------------
def add_stock_batch(*, product, warehouse, qty, unit_cost, user_id=None,
                    source="opening", serials=None):
    """Create a batch + movement + serials WITHOUT posting a journal entry.

    Returns (batch, value_native, value_book). Used by receive_stock and by
    purchase invoices (which post their own entry with a different credit side).
    """
    qty = quantize_amount(qty)
    unit_cost = quantize_amount(unit_cost)
    cur = warehouse_currency(warehouse)
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    unit_cost_book = quantize_amount(unit_cost * rate)

    if product.track_serial:
        serials = [s.strip() for s in (serials or []) if s and s.strip()]
        if len(serials) != int(qty):
            raise SerialError(
                f"المنتج يتتبع السريال — أدخل {int(qty)} سريالًا (أُدخل {len(serials)}).")
        if len(set(serials)) != len(serials):
            raise SerialError("يوجد سريال مكرر في الإدخال.")  # §16.15
        for s in serials:
            if db.session.scalar(db.select(ProductSerial)
                                 .filter_by(product_id=product.id, serial=s)):
                raise SerialError(f"السريال {s} مسجّل من قبل.")  # §16.15

    batch = StockBatch(
        product_id=product.id, warehouse_id=warehouse.id, qty_received=qty,
        qty_remaining=qty, currency_code=cur, unit_cost=unit_cost,
        unit_cost_book=unit_cost_book, valuation_method=product.valuation_method,
        source_doc=source, created_by_id=user_id,
    )
    db.session.add(batch)
    db.session.flush()
    db.session.add(StockMovement(
        product_id=product.id, warehouse_id=warehouse.id, batch_id=batch.id,
        direction="in", qty=qty, unit_cost=unit_cost_book, doc_type=source,
        doc_id=batch.id))
    if product.track_serial:
        for s in serials:
            db.session.add(ProductSerial(
                product_id=product.id, serial=s, warehouse_id=warehouse.id,
                batch_id=batch.id, status="in_stock", received_doc=source))

    return batch, quantize_amount(qty * unit_cost), quantize_amount(qty * unit_cost_book)


def receive_stock(*, product, warehouse, qty, unit_cost, user_id=None,
                  source="opening", serials=None):
    """Add a batch and post an opening/receipt entry (inventory Dr / equity Cr)."""
    cur = warehouse_currency(warehouse)
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    batch, value, _ = add_stock_batch(product=product, warehouse=warehouse,
                                      qty=qty, unit_cost=unit_cost, user_id=user_id,
                                      source=source, serials=serials)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=warehouse.branch_id,
        source_doc_type="stock.receive", source_doc_id=batch.id,
        memo=f"استلام مخزون — {product.name_ar}", user_id=user_id,
        lines=[
            posting.line(posting.account_for("inventory"), currency=cur,
                         amount=value, side="debit", fx_rate=rate),
            posting.line(posting.account_for("opening.equity"), currency=cur,
                         amount=value, side="credit", fx_rate=rate),
        ],
    )
    posting.post_entry(e, user_id=user_id)
    audit.record(action="stock.receive", entity="stock_batch", entity_id=batch.id,
                 new={"product": product.name_ar, "qty": str(qty)})
    return batch


# --- issue ----------------------------------------------------------------
def issue_stock(*, product, warehouse, qty, user_id=None, doc_type="sale",
                doc_id=None, serials=None, allow_negative=False):
    """Consume `qty` and return book COGS. FIFO or WA per each batch's method
    (§7.3). For serial products, consume exactly the given serials."""
    qty = quantize_amount(qty)

    if product.track_serial:
        return _issue_by_serial(product, warehouse, qty, serials, doc_type,
                                doc_id, user_id)

    batches = db.session.scalars(
        db.select(StockBatch)
        .filter(StockBatch.product_id == product.id,
                StockBatch.warehouse_id == warehouse.id,
                StockBatch.qty_remaining > 0)
        .order_by(StockBatch.created_at, StockBatch.id)
        .with_for_update()
    ).all()

    available = sum((b.qty_remaining for b in batches), Decimal("0"))
    if available < qty and not allow_negative:
        raise OutOfStockError(
            f"الكمية غير كافية لـ {product.name_ar}: المتاح {quantize_amount(available)}"
            f"، المطلوب {qty}.")

    # Weighted-average pool cost (over WA batches) computed before consuming.
    wa_batches = [b for b in batches if b.valuation_method == "WA"]
    wa_qty = sum((b.qty_remaining for b in wa_batches), Decimal("0"))
    wa_val = sum((b.qty_remaining * b.unit_cost_book for b in wa_batches), Decimal("0"))
    wa_avg = quantize_amount(wa_val / wa_qty) if wa_qty else Decimal("0")

    remaining = qty
    total_cost = Decimal("0")
    for b in batches:
        if remaining <= 0:
            break
        take = min(b.qty_remaining, remaining)
        cost_unit = wa_avg if b.valuation_method == "WA" else b.unit_cost_book
        b.qty_remaining = quantize_amount(b.qty_remaining - take)
        total_cost += quantize_amount(take * cost_unit)
        remaining = quantize_amount(remaining - take)
        db.session.add(StockMovement(
            product_id=product.id, warehouse_id=warehouse.id, batch_id=b.id,
            direction="out", qty=take, unit_cost=cost_unit,
            doc_type=doc_type, doc_id=doc_id))
    db.session.flush()
    return quantize_amount(total_cost)


def _issue_by_serial(product, warehouse, qty, serials, doc_type, doc_id, user_id):
    serials = [s.strip() for s in (serials or []) if s and s.strip()]
    if len(serials) != int(qty):
        raise SerialError(f"أدخل {int(qty)} سريالًا للبيع.")
    total_cost = Decimal("0")
    for s in serials:
        ps = db.session.scalar(db.select(ProductSerial).filter_by(
            product_id=product.id, serial=s).with_for_update())
        if ps is None:
            raise SerialError(f"السريال {s} غير موجود.")
        if ps.status == "sold":  # §16.14
            raise SerialError(
                f"السريال {s} مُباع بالفعل"
                + (f" في الفاتورة #{ps.sold_sale_id}." if ps.sold_sale_id else "."))
        batch = db.session.get(StockBatch, ps.batch_id)
        if batch and batch.qty_remaining > 0:
            batch.qty_remaining = quantize_amount(batch.qty_remaining - 1)
            cost = batch.unit_cost_book
        else:
            cost = Decimal("0")
        total_cost += quantize_amount(cost)
        ps.status = "sold"
        if doc_type == "sale":
            ps.sold_sale_id = doc_id
        db.session.add(StockMovement(
            product_id=product.id, warehouse_id=warehouse.id,
            batch_id=ps.batch_id, direction="out", qty=1, unit_cost=cost,
            doc_type=doc_type, doc_id=doc_id))
    db.session.flush()
    return quantize_amount(total_cost)


# --- cash sale ------------------------------------------------------------
def cash_sale(*, warehouse, treasury, lines, user_id=None, allow_below_min=False):
    """Record a cash sale (spec §10, §11, §13). Currency = the drawer's.

    lines: list of (product, qty, unit_price) or (product, qty, price, serials).
    """
    if not lines:
        raise EmptySaleError("لا توجد أصناف في البيع.")

    norm = []
    for ln in lines:
        product, qty, price = ln[0], ln[1], ln[2]
        serials = ln[3] if len(ln) > 3 else None
        norm.append((product, quantize_amount(qty), quantize_amount(price), serials))

    for product, qty, price, _ in norm:
        if price < quantize_amount(product.min_price) and not allow_below_min:
            raise MinPriceViolation(
                f"سعر {product.name_ar} ({price}) أقل من الحد الأدنى "
                f"({quantize_amount(product.min_price)}).")

    cur = treasury.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)

    sale = Sale(
        number=next_number(warehouse.branch_id, "SALE", prefix="INV-"),
        date=date.today(), branch_id=warehouse.branch_id,
        warehouse_id=warehouse.id, treasury_id=treasury.id, currency_code=cur,
        below_min_used=allow_below_min, created_by_id=user_id)
    db.session.add(sale)
    db.session.flush()

    total = Decimal("0")
    total_cost = Decimal("0")  # book
    for product, qty, price, serials in norm:
        line_total = quantize_amount(qty * price)
        cogs = issue_stock(product=product, warehouse=warehouse, qty=qty,
                           user_id=user_id, doc_type="sale", doc_id=sale.id,
                           serials=serials)
        unit_cost = quantize_amount(cogs / qty) if qty else Decimal("0")
        db.session.add(SaleLine(
            sale_id=sale.id, product_id=product.id, qty=qty, unit_price=price,
            line_total=line_total, unit_cost=unit_cost, cogs=cogs))
        total += line_total
        total_cost += cogs

    sale.total = quantize_amount(total)
    sale.total_cost = quantize_amount(total_cost)

    e = posting.build_entry(
        entry_date=sale.date, branch_id=warehouse.branch_id,
        source_doc_type="sale.cash", source_doc_id=sale.id,
        memo=f"بيع نقدي {sale.number}", user_id=user_id,
        lines=[
            posting.line(treasury.account_id, currency=cur, amount=sale.total,
                         side="debit", fx_rate=rate),
            posting.line(posting.account_for("sales.revenue"), currency=cur,
                         amount=sale.total, side="credit", fx_rate=rate),
            posting.line(posting.account_for("cogs"), currency=BOOK,
                         amount=sale.total_cost, side="debit", fx_rate=1),
            posting.line(posting.account_for("inventory"), currency=BOOK,
                         amount=sale.total_cost, side="credit", fx_rate=1),
        ],
    )
    posting.post_entry(e, user_id=user_id)
    sale.journal_entry_id = e.id
    db.session.flush()

    if allow_below_min:
        audit.record(action="sale.below_min", entity="sale", entity_id=sale.id,
                     new={"number": sale.number}, branch_id=warehouse.branch_id)
    audit.record(action="sale.cash", entity="sale", entity_id=sale.id,
                 new={"number": sale.number, "total": str(sale.total)},
                 branch_id=warehouse.branch_id)
    return sale


# --- reorder alerts (spec §7.5) -------------------------------------------
def reorder_alerts():
    """Products whose total on-hand is at or below their reorder level."""
    out = []
    products = db.session.scalars(
        db.select(Product).filter(Product.reorder_level > 0)
    ).all()
    for p in products:
        on_hand = db.session.scalar(
            db.select(db.func.coalesce(db.func.sum(StockBatch.qty_remaining), 0))
            .filter_by(product_id=p.id))
        on_hand = quantize_amount(on_hand)
        if on_hand <= quantize_amount(p.reorder_level):
            out.append({"product": p, "on_hand": on_hand})
    return out


# --- stocktake (spec §7 الجرد) --------------------------------------------
def open_stocktake(*, warehouse, user_id=None):
    st = Stocktake(
        number=next_number(warehouse.branch_id, "STK", prefix="STK-"),
        warehouse_id=warehouse.id, status="open", created_by_id=user_id)
    db.session.add(st)
    db.session.flush()
    for p in db.session.scalars(db.select(Product)).all():
        sys_qty = qty_on_hand(p.id, warehouse.id)
        st.lines.append(StocktakeLine(
            product_id=p.id, system_qty=sys_qty, counted_qty=sys_qty,
            unit_cost_book=avg_unit_cost_book(p.id, warehouse.id)))
    db.session.flush()
    audit.record(action="stocktake.open", entity="stocktake", entity_id=st.id)
    return st


def save_counts(stocktake, counts: dict):
    if stocktake.status != "open":
        raise StocktakeError("لا يمكن تعديل جرد معتمد.")
    for line in stocktake.lines:
        if line.product_id in counts:
            line.counted_qty = quantize_amount(counts[line.product_id])
    db.session.flush()


def approve_stocktake(stocktake, *, user_id, approver_id):
    """Post the adjustment (spec §7). Needs an approver (owner)."""
    if stocktake.status != "open":
        raise StocktakeError("الجرد معتمد بالفعل.")
    if approver_id is None:
        raise StocktakeError("تسوية الجرد تتطلب اعتماد المالك.")

    wh = stocktake.warehouse
    net_book = Decimal("0")
    for line in stocktake.lines:
        var = line.variance
        if var == 0:
            continue
        product = line.product
        if var > 0:
            # surplus: add a batch at the current book cost
            db.session.add(StockBatch(
                product_id=product.id, warehouse_id=wh.id, qty_received=var,
                qty_remaining=var, currency_code=BOOK,
                unit_cost=line.unit_cost_book, unit_cost_book=line.unit_cost_book,
                valuation_method=product.valuation_method, source_doc="stocktake"))
            db.session.add(StockMovement(
                product_id=product.id, warehouse_id=wh.id, direction="in",
                qty=var, unit_cost=line.unit_cost_book, doc_type="stocktake",
                doc_id=stocktake.id))
        else:
            issue_stock(product=product, warehouse=wh, qty=-var, user_id=user_id,
                        doc_type="stocktake", doc_id=stocktake.id,
                        allow_negative=True)
        net_book += quantize_amount(var * line.unit_cost_book)

    db.session.flush()
    net_book = quantize_amount(net_book)
    if net_book != 0:
        if net_book > 0:
            lines = [
                posting.line(posting.account_for("inventory"), currency=BOOK,
                             amount=net_book, side="debit", fx_rate=1),
                posting.line(posting.account_for("inventory.adjust.gain"),
                             currency=BOOK, amount=net_book, side="credit", fx_rate=1),
            ]
        else:
            lines = [
                posting.line(posting.account_for("inventory.adjust.loss"),
                             currency=BOOK, amount=-net_book, side="debit", fx_rate=1),
                posting.line(posting.account_for("inventory"), currency=BOOK,
                             amount=-net_book, side="credit", fx_rate=1),
            ]
        e = posting.build_entry(
            entry_date=date.today(), branch_id=wh.branch_id,
            source_doc_type="stocktake", source_doc_id=stocktake.id,
            memo=f"تسوية جرد {stocktake.number}", user_id=user_id, lines=lines)
        posting.post_entry(e, user_id=user_id)
        stocktake.journal_entry_id = e.id

    stocktake.status = "approved"
    stocktake.approved_by_id = approver_id
    db.session.flush()
    audit.record(action="stocktake.approve", entity="stocktake",
                 entity_id=stocktake.id, new={"net_book": str(net_book)})
    return stocktake


# --- damage / loss (spec INV-07) ------------------------------------------
def write_off(*, product, warehouse, qty, reason=None, user_id=None):
    """Record damaged/lost stock: issue it out and post a loss entry (§INV-07)."""
    cogs = issue_stock(product=product, warehouse=warehouse, qty=qty, user_id=user_id,
                       doc_type="damage", doc_id=None)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=warehouse.branch_id,
        source_doc_type="stock.damage", memo=reason or f"تالف/فقد — {product.name_ar}",
        user_id=user_id,
        lines=[
            posting.line(posting.account_for("inventory.adjust.loss"), currency=BOOK,
                         amount=cogs, side="debit", fx_rate=1),
            posting.line(posting.account_for("inventory"), currency=BOOK,
                         amount=cogs, side="credit", fx_rate=1),
        ])
    posting.post_entry(e, user_id=user_id)
    audit.record(action="stock.damage", entity="product", entity_id=product.id,
                 new={"qty": str(qty), "reason": reason})
    return e


def barcode_svg(code: str, *, writer_options=None) -> str | None:
    """Render a Code128 barcode as an inline SVG string for label printing
    (spec INV-01.7). Returns None if the code is empty or invalid."""
    if not code:
        return None
    import io
    try:
        import barcode
        from barcode.writer import SVGWriter
    except ImportError:  # pragma: no cover - dependency ships in requirements
        return None
    try:
        opts = {"module_height": 8.0, "font_size": 8, "text_distance": 3.0,
                "quiet_zone": 2.0}
        if writer_options:
            opts.update(writer_options)
        buf = io.BytesIO()
        barcode.get("code128", str(code), writer=SVGWriter()).write(buf, opts)
        svg = buf.getvalue().decode("utf-8")
        # keep only the <svg>...</svg> body (drop the XML/doctype header)
        i = svg.find("<svg")
        return svg[i:] if i >= 0 else svg
    except Exception:  # noqa: BLE001 - bad code shouldn't break the page
        return None
