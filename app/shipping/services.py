"""Transfer-permit lifecycle + landed-cost allocation (spec §8)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.accounting import posting
from app.core import audit, fx
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.core.numbering import next_number
from app.extensions import db
from app.inventory import services as inv
from app.inventory.models import StockBatch, StockMovement
from app.shipping.models import TransferExpense, TransferLine, TransferPermit

BOOK = "AED"


class TransferStateError(Exception):
    pass


class NotEnoughStockError(Exception):
    pass


# --- create / cancel ------------------------------------------------------
def create_permit(*, from_warehouse, to_warehouse, lines, allocation_method="value",
                  fx_rate=None, user_id=None):
    """Draft a permit. `lines`: [(product, qty)]."""
    if from_warehouse.id == to_warehouse.id:
        raise ValueError("لا يمكن التحويل إلى نفس المخزن.")
    dest_cur = inv.warehouse_currency(to_warehouse)
    rate = quantize_rate(fx_rate) if fx_rate else (
        fx.rate_to_book(dest_cur) or quantize_rate(1))
    permit = TransferPermit(
        number=next_number(from_warehouse.branch_id, "TRF", prefix="TRF-"),
        from_warehouse_id=from_warehouse.id, to_warehouse_id=to_warehouse.id,
        status="draft", allocation_method=allocation_method, fx_rate=rate,
        created_by_id=user_id)
    db.session.add(permit)
    db.session.flush()
    for product, qty in lines:
        permit.lines.append(TransferLine(product_id=product.id,
                                         qty_sent=quantize_amount(qty)))
    db.session.flush()
    audit.record(action="transfer.create", entity="transfer_permit",
                 entity_id=permit.id, new={"number": permit.number})
    return permit


def cancel_permit(permit, *, user_id=None):
    if permit.status != "draft":  # only before send (§8 states)
        raise TransferStateError("لا يمكن الإلغاء إلا قبل الإرسال.")
    permit.status = "cancelled"
    audit.record(action="transfer.cancel", entity="transfer_permit",
                 entity_id=permit.id)


# --- send -----------------------------------------------------------------
def send_permit(permit, *, user_id=None):
    """Issue stock from the source; its book value moves to goods-in-transit."""
    if permit.status != "draft":
        raise TransferStateError("تم إرسال الإذن من قبل.")
    src = permit.from_warehouse
    total_book = Decimal("0")
    for line in permit.lines:
        product = line.product
        avail = inv.qty_on_hand(product.id, src.id)
        if avail < line.qty_sent:
            raise NotEnoughStockError(
                f"الكمية غير كافية لـ {product.name_ar}: المتاح {avail}.")
        cogs = inv.issue_stock(product=product, warehouse=src, qty=line.qty_sent,
                               user_id=user_id, doc_type="transfer",
                               doc_id=permit.id)
        line.unit_cost_book = (quantize_amount(cogs / line.qty_sent)
                               if line.qty_sent else Decimal("0"))
        total_book += cogs
    total_book = quantize_amount(total_book)

    e = posting.build_entry(
        entry_date=date.today(), branch_id=src.branch_id,
        source_doc_type="transfer.send", source_doc_id=permit.id,
        memo=f"إرسال إذن تحويل {permit.number}", user_id=user_id,
        lines=[
            posting.line(posting.account_for("goods_in_transit"), currency=BOOK,
                         amount=total_book, side="debit", fx_rate=1),
            posting.line(posting.account_for("inventory"), currency=BOOK,
                         amount=total_book, side="credit", fx_rate=1),
        ])
    posting.post_entry(e, user_id=user_id)
    permit.status = "sent"
    permit.sent_at = datetime.utcnow()
    db.session.flush()
    audit.record(action="transfer.send", entity="transfer_permit",
                 entity_id=permit.id, new={"goods_book": str(total_book)})
    return permit


# --- expenses + allocation ------------------------------------------------
def add_expense(permit, *, kind, amount_book, paid_from_account_id, note=None,
                user_id=None):
    """Record a shipping/customs cost, capitalized into goods-in-transit (§13)."""
    if permit.status not in ("sent", "received"):
        raise TransferStateError("تُسجَّل المصاريف بعد الإرسال.")
    amount = quantize_amount(amount_book)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=permit.from_warehouse.branch_id,
        source_doc_type=f"transfer.{kind}", source_doc_id=permit.id,
        memo=f"{'شحن' if kind == 'shipping' else 'جمارك'} إذن {permit.number}",
        user_id=user_id,
        lines=[
            posting.line(posting.account_for("goods_in_transit"), currency=BOOK,
                         amount=amount, side="debit", fx_rate=1),
            posting.line(paid_from_account_id, currency=BOOK, amount=amount,
                         side="credit", fx_rate=1),
        ])
    posting.post_entry(e, user_id=user_id)
    exp = TransferExpense(permit_id=permit.id, kind=kind, amount_book=amount,
                          paid_from_account_id=paid_from_account_id, note=note,
                          journal_entry_id=e.id)
    db.session.add(exp)
    db.session.flush()
    allocate(permit)  # re-spread across items whenever an expense is added
    audit.record(action=f"transfer.{kind}", entity="transfer_permit",
                 entity_id=permit.id, new={"amount": str(amount)})
    return exp


def _weight_of(line):
    return to_decimal(line.qty_sent) * to_decimal(line.product.weight or 0)


def allocate(permit):
    """Spread shipping & customs over the items by the permit's method (§8)."""
    method = permit.allocation_method
    shipping = quantize_amount(sum(
        (e.amount_book for e in permit.expenses if e.kind == "shipping"), Decimal("0")))
    customs = quantize_amount(sum(
        (e.amount_book for e in permit.expenses if e.kind == "customs"), Decimal("0")))

    def basis(line):
        if method == "quantity":
            return to_decimal(line.qty_sent)
        if method == "weight":
            return _weight_of(line)
        return to_decimal(line.qty_sent) * to_decimal(line.unit_cost_book)  # value

    total_basis = sum((basis(l) for l in permit.lines), Decimal("0"))
    for line in permit.lines:
        if total_basis > 0:
            frac = basis(line) / total_basis
        else:
            frac = Decimal("0")
        line.alloc_shipping_book = quantize_amount(shipping * frac)
        line.alloc_customs_book = quantize_amount(customs * frac)
    db.session.flush()


# --- receive --------------------------------------------------------------
def receive_permit(permit, *, received=None, partial=False, user_id=None):
    """Land the goods in the destination at original cost + freight/customs share.

    ``received``: {line_id: qty received *this time*}. Defaults to the whole
    outstanding quantity of each line.

    Two modes (spec TRF-05.6):
      - ``partial=True`` — receive part of the shipment now; goods-in-transit is
        credited only for what arrived and the permit stays *sent* so the rest can
        be received later. Quantities accumulate across calls.
      - ``partial=False`` (default, final receipt) — any quantity still outstanding
        after this receipt is booked as a transit loss so goods-in-transit clears
        and the permit becomes *received* (§8.7)."""
    if permit.status != "sent":
        raise TransferStateError("الإذن ليس في حالة الإرسال.")
    dest = permit.to_warehouse
    dest_cur = inv.warehouse_currency(dest)
    received = received or {}

    received_book = Decimal("0")
    any_qty = False
    for line in permit.lines:
        outstanding = quantize_amount(line.qty_sent - line.qty_received)
        default_qty = outstanding
        qty_recv = quantize_amount(received.get(line.id, default_qty))
        if qty_recv < 0:
            raise TransferStateError("الكمية المستلمة لا يمكن أن تكون سالبة.")
        if qty_recv > outstanding:
            raise TransferStateError(
                f"الكمية المستلمة لصنف {line.product.name_ar} تتجاوز المتبقي "
                f"({outstanding}).")
        line.qty_received = quantize_amount(line.qty_received + qty_recv)
        if qty_recv <= 0:
            continue
        any_qty = True
        landed_book = line.final_unit_cost_book  # per unit, incl. freight+customs
        native = line.final_unit_cost_native(permit.fx_rate)
        batch = StockBatch(
            product_id=line.product_id, warehouse_id=dest.id, qty_received=qty_recv,
            qty_remaining=qty_recv, currency_code=dest_cur, unit_cost=native,
            unit_cost_book=landed_book,
            valuation_method=line.product.valuation_method,
            source_doc=f"transfer:{permit.id}", created_by_id=user_id)
        db.session.add(batch)
        db.session.flush()
        db.session.add(StockMovement(
            product_id=line.product_id, warehouse_id=dest.id, batch_id=batch.id,
            direction="in", qty=qty_recv, unit_cost=landed_book,
            doc_type="transfer", doc_id=permit.id))
        received_book += quantize_amount(qty_recv * landed_book)

    received_book = quantize_amount(received_book)
    if not any_qty:
        raise TransferStateError("لم يتم إدخال أي كمية مستلمة.")

    # outstanding book value still in transit after this receipt
    outstanding_book = quantize_amount(sum(
        ((l.qty_sent - l.qty_received) * l.final_unit_cost_book
         for l in permit.lines), Decimal("0")))

    lines = [posting.line(posting.account_for("inventory"), currency=BOOK,
                          amount=received_book, side="debit", fx_rate=1)]
    git_credit = received_book
    shortfall = Decimal("0")
    if not partial and outstanding_book > 0:  # final receipt: rest is a loss (§8.7)
        shortfall = outstanding_book
        lines.append(posting.line(posting.account_for("inventory.adjust.loss"),
                                  currency=BOOK, amount=shortfall, side="debit", fx_rate=1))
        git_credit = quantize_amount(received_book + shortfall)
    lines.append(posting.line(posting.account_for("goods_in_transit"),
                              currency=BOOK, amount=git_credit, side="credit", fx_rate=1))

    e = posting.build_entry(
        entry_date=date.today(), branch_id=dest.branch_id,
        source_doc_type="transfer.receive", source_doc_id=permit.id,
        memo=f"استلام إذن تحويل {permit.number}", user_id=user_id, lines=lines)
    posting.post_entry(e, user_id=user_id)

    fully_received = all(l.qty_received >= l.qty_sent for l in permit.lines)
    if partial and not fully_received:
        permit.status = "sent"  # more still on the way
    else:
        permit.status = "received"
        permit.received_at = datetime.utcnow()
    db.session.flush()
    audit.record(action="transfer.receive", entity="transfer_permit",
                 entity_id=permit.id, new={"received_book": str(received_book),
                                           "shortfall": str(shortfall),
                                           "partial": partial})
    return permit


def close_permit(permit, *, user_id=None):
    if permit.status != "received":
        raise TransferStateError("يُغلق الإذن بعد الاستلام.")
    permit.status = "closed"
    permit.closed_at = datetime.utcnow()
    audit.record(action="transfer.close", entity="transfer_permit",
                 entity_id=permit.id)


# --- reports --------------------------------------------------------------
def in_transit_qty(product_id, warehouse_id=None):
    """Units sent but not yet received (goods in transit)."""
    q = (db.select(db.func.coalesce(db.func.sum(TransferLine.qty_sent), 0))
         .join(TransferPermit, TransferPermit.id == TransferLine.permit_id)
         .filter(TransferLine.product_id == product_id,
                 TransferPermit.status == "sent"))
    if warehouse_id:
        q = q.filter(TransferPermit.from_warehouse_id == warehouse_id)
    return quantize_amount(db.session.scalar(q))
