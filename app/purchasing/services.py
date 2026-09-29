"""Supplier & purchasing services (spec §9)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.accounting import posting
from app.accounting.models import Account, JournalEntry, JournalLine
from app.core import audit, fx
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.core.numbering import next_number
from app.extensions import db
from app.inventory import services as inv
from app.purchasing.models import (
    PaymentAllocation,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    Supplier,
    SupplierPayment,
)

BOOK = "AED"


class InsufficientCustodyError(Exception):
    pass


def _child_account(parent_code, name_ar, user_id=None):
    parent = db.session.scalar(db.select(Account).filter_by(code=parent_code))
    count = db.session.scalar(
        db.select(db.func.count(Account.id))
        .filter(Account.code.like(f"{parent_code}-%")))
    acc = Account(code=f"{parent_code}-{(count or 0) + 1:03d}", name_ar=name_ar,
                  name_en=name_ar, type=parent.type if parent else "liability",
                  is_postable=True, parent_id=parent.id if parent else None,
                  created_by_id=user_id)
    db.session.add(acc)
    db.session.flush()
    return acc


# --- suppliers ------------------------------------------------------------
def create_supplier(*, name_ar, phone="", currency_code="AED",
                    payment_terms_days=0, opening_balance=0, branch_id=None,
                    user_id=None):
    acc = _child_account("2101", f"مورد: {name_ar}", user_id)
    s = Supplier(name_ar=name_ar, phone=phone, currency_code=currency_code,
                 payment_terms_days=payment_terms_days,
                 opening_balance=quantize_amount(opening_balance),
                 account_id=acc.id, created_by_id=user_id)
    db.session.add(s)
    db.session.flush()
    ob = quantize_amount(opening_balance)
    if ob != 0:
        rate = fx.rate_to_book(currency_code) or quantize_rate(1)
        e = posting.build_entry(
            entry_date=date.today(), branch_id=branch_id,
            source_doc_type="supplier.opening", source_doc_id=s.id,
            memo=f"رصيد افتتاحي مورد — {name_ar}", user_id=user_id,
            lines=[
                posting.line(posting.account_for("opening.equity"),
                             currency=currency_code, amount=ob, side="debit", fx_rate=rate),
                posting.line(acc.id, currency=currency_code, amount=ob,
                             side="credit", fx_rate=rate),
            ])
        posting.post_entry(e, user_id=user_id)
    audit.record(action="supplier.create", entity="supplier", entity_id=s.id,
                 new={"name": name_ar})
    return s


def ap_balance(supplier: Supplier) -> Decimal:
    """What we owe the supplier, in the supplier's currency (credits - debits)."""
    dr, cr = db.session.execute(
        db.select(
            db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == supplier.account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(cr) - to_decimal(dr))


def invoice_outstanding(invoice: PurchaseInvoice) -> Decimal:
    paid = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0))
        .filter_by(invoice_id=invoice.id))
    return quantize_amount(to_decimal(invoice.total) - to_decimal(paid))


# --- purchase invoice -----------------------------------------------------
def create_purchase_invoice(*, supplier, warehouse, lines, payment_type,
                            pay_from_account_id=None, custody=None,
                            due_date=None, user_id=None):
    """Post a purchase invoice: receive stock + book it (spec §9.1).

    lines: [(product, qty, unit_cost)]. Currency = the warehouse's.
    payment_type: cash (from a treasury/bank), credit (supplier AP), or custody.
    """
    cur = inv.warehouse_currency(warehouse)
    rate = fx.rate_to_book(cur) or quantize_rate(1)

    total = Decimal("0")
    total_book = Decimal("0")
    batch_specs = []
    for product, qty, cost in lines:
        qty = quantize_amount(qty)
        cost = quantize_amount(cost)
        total += quantize_amount(qty * cost)
        batch_specs.append((product, qty, cost))
    total = quantize_amount(total)
    total_book = quantize_amount(total * rate)

    if payment_type == "custody":
        if custody is None:
            raise ValueError("اختر العهدة.")
        avail = _custody_available(custody)
        if total > avail:  # §12.3
            raise InsufficientCustodyError(
                f"قيمة الشراء {total} تتجاوز رصيد العهدة المتاح {avail}.")

    invoice = PurchaseInvoice(
        number=next_number(warehouse.branch_id, "PINV", prefix="PUR-"),
        date=date.today(), branch_id=warehouse.branch_id,
        supplier_id=supplier.id if supplier else None, warehouse_id=warehouse.id,
        payment_type=payment_type, pay_from_account_id=pay_from_account_id,
        custody_id=custody.id if custody else None, currency_code=cur,
        fx_rate=rate, total=total, total_book=total_book,
        due_date=due_date, created_by_id=user_id)
    db.session.add(invoice)
    db.session.flush()

    for product, qty, cost in batch_specs:
        inv.add_stock_batch(product=product, warehouse=warehouse, qty=qty,
                            unit_cost=cost, user_id=user_id,
                            source=f"purchase:{invoice.id}")
        db.session.add(PurchaseInvoiceLine(
            invoice_id=invoice.id, product_id=product.id, qty=qty,
            unit_cost=cost, line_total=quantize_amount(qty * cost)))

    # credit side
    if payment_type == "cash":
        credit_account = pay_from_account_id
    elif payment_type == "credit":
        credit_account = supplier.account_id
    else:  # custody
        credit_account = custody.account_id

    e = posting.build_entry(
        entry_date=invoice.date, branch_id=warehouse.branch_id,
        source_doc_type="purchase.invoice", source_doc_id=invoice.id,
        memo=f"فاتورة شراء {invoice.number}", user_id=user_id,
        lines=[
            posting.line(posting.account_for("inventory"), currency=cur,
                         amount=total, side="debit", fx_rate=rate),
            posting.line(credit_account, currency=cur, amount=total,
                         side="credit", fx_rate=rate),
        ])
    posting.post_entry(e, user_id=user_id)
    invoice.journal_entry_id = e.id

    if payment_type == "custody":
        from app.custody.models import CustodyTxn
        db.session.add(CustodyTxn(
            custody_id=custody.id, date=invoice.date, kind="purchase",
            amount=total, memo=f"شراء {invoice.number}",
            ref_doc=invoice.number, journal_entry_id=e.id, created_by_id=user_id))

    db.session.flush()
    audit.record(action="purchase.invoice", entity="purchase_invoice",
                 entity_id=invoice.id, new={"number": invoice.number,
                                            "total": str(total)},
                 branch_id=warehouse.branch_id)
    return invoice


def _custody_available(custody):
    from app.custody.services import available
    return available(custody)


# --- supplier payment (spec §9.3, §9.4 realized FX) -----------------------
def create_supplier_payment(*, supplier, pay_from_account_id, amount,
                            allocations=None, payment_rate=None, branch_id=None,
                            user_id=None):
    """Pay a supplier; allocate to invoices; book realized FX if the rate moved.

    allocations: [(invoice, alloc_amount)] in the supplier's currency.
    """
    cur = supplier.currency_code
    pay_rate = quantize_rate(payment_rate) if payment_rate else (
        fx.rate_to_book(cur) or quantize_rate(1))
    amount = quantize_amount(amount)
    allocations = allocations or []

    payment = SupplierPayment(
        number=next_number(branch_id, "SPAY", prefix="PAY-"),
        date=date.today(), supplier_id=supplier.id, amount=amount,
        currency_code=cur, fx_rate=pay_rate,
        pay_from_account_id=pay_from_account_id, created_by_id=user_id)
    db.session.add(payment)
    db.session.flush()

    lines = []
    book_ap = Decimal("0")
    allocated = Decimal("0")
    for invoice, alloc in allocations:
        alloc = quantize_amount(alloc)
        if alloc <= 0:
            continue
        allocated += alloc
        inv_rate = quantize_rate(invoice.fx_rate)
        lines.append(posting.line(supplier.account_id, currency=cur, amount=alloc,
                                  side="debit", fx_rate=inv_rate))
        book_ap += quantize_amount(alloc * inv_rate)
        db.session.add(PaymentAllocation(payment_id=payment.id,
                                         invoice_id=invoice.id, amount=alloc))

    remainder = quantize_amount(amount - allocated)
    if remainder > 0:  # on-account payment, no FX
        lines.append(posting.line(supplier.account_id, currency=cur,
                                  amount=remainder, side="debit", fx_rate=pay_rate))
        book_ap += quantize_amount(remainder * pay_rate)

    book_cash = quantize_amount(amount * pay_rate)
    lines.append(posting.line(pay_from_account_id, currency=cur, amount=amount,
                              side="credit", fx_rate=pay_rate))

    diff = quantize_amount(book_cash - book_ap)  # book paid vs liability cleared
    if diff > 0:
        lines.append(posting.line(posting.account_for("fx.loss"), currency=BOOK,
                                  amount=diff, side="debit", fx_rate=1))
    elif diff < 0:
        lines.append(posting.line(posting.account_for("fx.gain"), currency=BOOK,
                                  amount=-diff, side="credit", fx_rate=1))

    e = posting.build_entry(
        entry_date=payment.date, branch_id=branch_id,
        source_doc_type="supplier.payment", source_doc_id=payment.id,
        memo=f"سداد مورد {payment.number}", user_id=user_id, lines=lines)
    posting.post_entry(e, user_id=user_id)
    payment.journal_entry_id = e.id
    db.session.flush()
    audit.record(action="supplier.payment", entity="supplier_payment",
                 entity_id=payment.id, new={"amount": str(amount)})
    return payment


# --- aging & due alerts (spec §9.7, §9.8) ---------------------------------
def aging(as_of=None):
    """Open payables per supplier, bucketed by age of the invoice."""
    as_of = as_of or date.today()
    rows = []
    for s in db.session.scalars(db.select(Supplier)).all():
        buckets = {"0-30": Decimal("0"), "31-60": Decimal("0"),
                   "61-90": Decimal("0"), "90+": Decimal("0")}
        total = Decimal("0")
        invoices = db.session.scalars(
            db.select(PurchaseInvoice).filter_by(supplier_id=s.id,
                                                 payment_type="credit")).all()
        for inv_ in invoices:
            out = invoice_outstanding(inv_)
            if out <= 0:
                continue
            age = (as_of - inv_.date).days
            key = ("0-30" if age <= 30 else "31-60" if age <= 60
                   else "61-90" if age <= 90 else "90+")
            buckets[key] += out
            total += out
        if total > 0:
            rows.append({"supplier": s, "buckets": buckets, "total": total})
    return rows


def due_soon(within_days=7):
    """Credit invoices with an outstanding balance due within N days."""
    limit = date.today() + timedelta(days=within_days)
    out = []
    for inv_ in db.session.scalars(
        db.select(PurchaseInvoice).filter(PurchaseInvoice.payment_type == "credit",
                                          PurchaseInvoice.due_date.isnot(None),
                                          PurchaseInvoice.due_date <= limit)).all():
        bal = invoice_outstanding(inv_)
        if bal > 0:
            out.append({"invoice": inv_, "outstanding": bal})
    return out


# --- purchase orders (spec PUR-01) ----------------------------------------
def create_order(*, supplier, warehouse, lines, expected_date=None, user_id=None):
    from app.purchasing.models import PurchaseOrder, PurchaseOrderLine
    from app.inventory import services as inv
    order = PurchaseOrder(
        number=next_number(warehouse.branch_id, "PO", prefix="PO-"),
        supplier_id=supplier.id if supplier else None, warehouse_id=warehouse.id,
        currency_code=inv.warehouse_currency(warehouse), expected_date=expected_date,
        status="draft", created_by_id=user_id)
    db.session.add(order)
    db.session.flush()
    for product, qty, cost in lines:
        order.lines.append(PurchaseOrderLine(product_id=product.id,
                                             qty=quantize_amount(qty),
                                             unit_cost=quantize_amount(cost)))
    db.session.flush()
    audit.record(action="po.create", entity="purchase_order", entity_id=order.id,
                 new={"number": order.number})
    return order


def approve_order(order, *, user_id=None):
    if order.status != "draft":
        raise ValueError("لا يمكن اعتماد أمر ليس مسودة.")
    order.status = "approved"
    audit.record(action="po.approve", entity="purchase_order", entity_id=order.id)


def cancel_order(order, *, user_id=None):
    if order.status in ("received",):
        raise ValueError("لا يمكن إلغاء أمر مستلم.")
    order.status = "cancelled"


def convert_order_to_invoice(order, *, payment_type="credit", pay_from_account_id=None,
                             user_id=None):
    """Turn an approved PO into a real purchase invoice (PUR-01.4)."""
    from app.inventory.models import Product
    if order.status not in ("draft", "approved"):
        raise ValueError("لا يمكن تحويل هذا الأمر.")
    if payment_type == "credit" and order.supplier is None:
        raise ValueError("الشراء الآجل يتطلب مورّدًا. اختر موردًا للأمر أو طريقة دفع أخرى.")
    lines = [(db.session.get(Product, l.product_id), l.qty, l.unit_cost)
             for l in order.lines]
    invoice = create_purchase_invoice(
        supplier=order.supplier, warehouse=order.warehouse, lines=lines,
        payment_type=payment_type, pay_from_account_id=pay_from_account_id,
        user_id=user_id)
    order.status = "received"
    order.invoice_id = invoice.id
    db.session.flush()
    audit.record(action="po.convert", entity="purchase_order", entity_id=order.id,
                 new={"invoice": invoice.number})
    return invoice


# --- purchase return (spec PUR-06) ----------------------------------------
def create_purchase_return(*, invoice, user_id=None):
    """Return a full purchase invoice: stock out at original cost, reduce AP or
    refund the treasury (spec PUR-06)."""
    from app.inventory import services as inv
    from app.inventory.models import Product
    wh = invoice.warehouse
    cur = invoice.currency_code
    rate = quantize_rate(invoice.fx_rate)
    total_book = Decimal("0")
    for l in invoice.lines:
        # remove the stock we received, at its book cost
        cogs = inv.issue_stock(product=db.session.get(Product, l.product_id),
                               warehouse=wh, qty=l.qty, user_id=user_id,
                               doc_type="purchase.return", doc_id=invoice.id,
                               allow_negative=True)
        total_book += cogs
    total = quantize_amount(invoice.total)
    # credit side: supplier AP (credit purchase) or the treasury (cash purchase)
    if invoice.payment_type == "credit" and invoice.supplier:
        money_account = invoice.supplier.account_id
    else:
        money_account = invoice.pay_from_account_id or posting.account_for("cash.main")
    e = posting.build_entry(
        entry_date=date.today(), branch_id=invoice.branch_id,
        source_doc_type="purchase.return", source_doc_id=invoice.id,
        memo=f"مرتجع مشتريات للفاتورة {invoice.number}", user_id=user_id,
        lines=[
            posting.line(money_account, currency=cur, amount=total, side="debit", fx_rate=rate),
            posting.line(posting.account_for("inventory"), currency=BOOK,
                         amount=quantize_amount(total_book), side="credit", fx_rate=1),
        ])
    posting.post_entry(e, user_id=user_id)
    audit.record(action="purchase.return", entity="purchase_invoice",
                 entity_id=invoice.id, new={"number": invoice.number})
    return e
