"""Sales services (spec §10, §11)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.accounting import posting
from app.accounting.models import Account, JournalEntry, JournalLine
from app.core import audit, fx, settings
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.core.numbering import next_number
from app.extensions import db
from app.inventory import services as inv
from app.inventory.models import Sale, SaleLine
from app.sales.models import (
    Collection, Customer, Funder, FunderSettlement, InstallmentDue,
    InstallmentPlan, SalePayment, SalesReturn, SalesReturnLine, Shift,
)

BOOK = "AED"


class CreditLimitError(Exception):
    pass


class NoOpenShiftError(Exception):
    pass


class ShiftError(Exception):
    pass


class AlreadyReturnedError(Exception):
    pass


def _child_account(parent_code, name_ar, user_id=None):
    parent = db.session.scalar(db.select(Account).filter_by(code=parent_code))
    n = db.session.scalar(db.select(db.func.count(Account.id))
                          .filter(Account.code.like(f"{parent_code}-%")))
    acc = Account(code=f"{parent_code}-{(n or 0) + 1:03d}", name_ar=name_ar,
                  name_en=name_ar, type="asset", is_postable=True,
                  parent_id=parent.id if parent else None, created_by_id=user_id)
    db.session.add(acc)
    db.session.flush()
    return acc


# --- customers ------------------------------------------------------------
def create_customer(*, name_ar, phone="", type="retail", credit_limit=0,
                    opening_balance=0, currency_code="AED", branch_id=None,
                    user_id=None):
    acc = _child_account("1201", f"عميل: {name_ar}", user_id)
    c = Customer(name_ar=name_ar, phone=phone, type=type,
                 credit_limit=quantize_amount(credit_limit),
                 opening_balance=quantize_amount(opening_balance),
                 currency_code=currency_code, account_id=acc.id, created_by_id=user_id)
    db.session.add(c)
    db.session.flush()
    ob = quantize_amount(opening_balance)
    if ob != 0:
        rate = fx.rate_to_book(currency_code) or quantize_rate(1)
        e = posting.build_entry(
            entry_date=date.today(), branch_id=branch_id,
            source_doc_type="customer.opening", source_doc_id=c.id,
            memo=f"رصيد افتتاحي عميل — {name_ar}", user_id=user_id,
            lines=[
                posting.line(acc.id, currency=currency_code, amount=ob,
                             side="debit", fx_rate=rate),
                posting.line(posting.account_for("opening.equity"),
                             currency=currency_code, amount=ob, side="credit", fx_rate=rate),
            ])
        posting.post_entry(e, user_id=user_id)
    audit.record(action="customer.create", entity="customer", entity_id=c.id,
                 new={"name": name_ar})
    return c


def ar_balance(customer: Customer) -> Decimal:
    """What the customer owes us (debits - credits), in their currency."""
    dr, cr = db.session.execute(
        db.select(
            db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == customer.account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


# --- shifts (spec §11) ----------------------------------------------------
def open_shift(*, cashier_id, drawer, opening_cash, user_id=None):
    existing = db.session.scalar(
        db.select(Shift).filter_by(drawer_id=drawer.id, status="open"))
    if existing:  # one open shift per drawer (§11)
        raise ShiftError("يوجد وردية مفتوحة على هذا الدرج بالفعل.")
    sh = Shift(number=next_number(drawer.branch_id, "SHIFT", prefix="SH-"),
               cashier_id=cashier_id, drawer_id=drawer.id,
               opening_cash=quantize_amount(opening_cash), status="open",
               created_by_id=user_id)
    db.session.add(sh)
    db.session.flush()
    audit.record(action="shift.open", entity="shift", entity_id=sh.id)
    return sh


def open_shift_for(drawer_id):
    return db.session.scalar(db.select(Shift).filter_by(drawer_id=drawer_id,
                                                        status="open"))


def shift_cash_sales(shift: Shift) -> Decimal:
    """Cash collected in this shift (cash sales into the drawer)."""
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(Sale.total), 0))
        .filter(Sale.shift_id == shift.id, Sale.payment_type == "cash"))
    return quantize_amount(total)


def shift_cash_returns(shift: Shift) -> Decimal:
    """Cash refunded during this shift for its own cash sales (drawer outflow)."""
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(SalesReturn.total), 0))
        .join(Sale, Sale.id == SalesReturn.original_sale_id)
        .filter(Sale.shift_id == shift.id, Sale.payment_type == "cash"))
    return quantize_amount(total)


def shift_expected_cash(shift: Shift) -> Decimal:
    """Expected drawer cash = opening + cash sales − cash returns (spec §11.3)."""
    return quantize_amount(shift.opening_cash + shift_cash_sales(shift)
                           - shift_cash_returns(shift))


def close_shift(*, shift, counted_cash, user_id=None):
    if shift.status != "open":
        raise ShiftError("الوردية مغلقة.")
    counted = quantize_amount(counted_cash)
    expected = shift_expected_cash(shift)
    over_short = quantize_amount(counted - expected)  # + surplus / - shortage
    shift.counted_cash = counted
    shift.expected_cash = expected
    shift.over_short = over_short
    shift.status = "closed"
    shift.closed_at = datetime.utcnow()

    if over_short != 0:  # post the difference (spec §11.3, §16.6)
        drawer_acc = shift.drawer.account_id
        if over_short < 0:  # shortage: expense Dr / drawer Cr
            lines = [
                posting.line(posting.account_for("shift.shortage"), currency=BOOK,
                             amount=-over_short, side="debit", fx_rate=1),
                posting.line(drawer_acc, currency=BOOK, amount=-over_short,
                             side="credit", fx_rate=1)]
        else:  # surplus: drawer Dr / income Cr
            lines = [
                posting.line(drawer_acc, currency=BOOK, amount=over_short,
                             side="debit", fx_rate=1),
                posting.line(posting.account_for("shift.surplus"), currency=BOOK,
                             amount=over_short, side="credit", fx_rate=1)]
        e = posting.build_entry(
            entry_date=date.today(), branch_id=shift.drawer.branch_id,
            source_doc_type="shift.close", source_doc_id=shift.id,
            memo=f"عجز/زيادة وردية {shift.number}", user_id=user_id, lines=lines)
        posting.post_entry(e, user_id=user_id)
        shift.journal_entry_id = e.id
    db.session.flush()
    audit.record(action="shift.close", entity="shift", entity_id=shift.id,
                 new={"over_short": str(over_short)})
    return shift


# --- sale (cash / card / credit) with VAT (spec §10) ----------------------
def make_sale(*, warehouse, lines, payment_type="cash", treasury=None,
              bank_account=None, customer=None, funder=None, shift=None,
              installment=None, payments=None, discount=0, user_id=None,
              allow_below_min=False, allow_over_limit=False):
    """Post a sale. Payment types: cash (drawer), card (bank), credit (customer AR),
    funder (BNPL receivable), installment (customer AR + schedule).

    lines: [(product, qty, price)] or with a 4th serials element.
    installment: {count, first_due (date), down_payment, down_treasury} (optional).
    Applies the branch VAT rate if enabled (§10.6).
    """
    if not lines:
        raise inv.EmptySaleError("لا توجد أصناف.")

    norm = []
    for ln in lines:
        product, qty, price = ln[0], ln[1], ln[2]
        serials = ln[3] if len(ln) > 3 else None
        norm.append((product, quantize_amount(qty), quantize_amount(price), serials))

    for product, qty, price, _ in norm:
        if price < quantize_amount(product.min_price) and not allow_below_min:
            raise inv.MinPriceViolation(
                f"سعر {product.name_ar} أقل من الحد الأدنى.")

    # money destination
    if payment_type == "cash":
        if shift is None:
            raise NoOpenShiftError("لا يوجد وردية مفتوحة — افتح وردية قبل البيع.")
        dest_account = treasury.account_id
        cur = treasury.currency_code
    elif payment_type == "card":
        dest_account = bank_account.account_id
        cur = bank_account.currency_code
    elif payment_type in ("credit", "installment"):
        if customer is None:
            raise ValueError("البيع الآجل يتطلب عميلًا.")
        dest_account = customer.account_id
        cur = customer.currency_code
    elif payment_type == "funder":
        if funder is None:
            raise ValueError("البيع عبر جهة تمويل يتطلب اختيار الجهة.")
        dest_account = funder.account_id  # receivable on the funder (§10 BNPL)
        cur = funder.currency_code
    elif payment_type == "split":
        # split/multi-method payment (spec POS-03). Tenders sum to the total.
        if not payments:
            raise ValueError("الدفع المقسّم يتطلب أطراف دفع.")
        if any(p[0] == "cash" for p in payments) and shift is None:
            raise NoOpenShiftError("الدفع النقدي يتطلب وردية مفتوحة.")
        dest_account = None
        cur = treasury.currency_code if treasury else inv.warehouse_currency(warehouse)
    else:
        raise ValueError(f"طريقة دفع غير مدعومة: {payment_type}")
    rate = fx.rate_to_book(cur) or quantize_rate(1)

    # tax rate from branch settings (§10.6)
    tax_rate = Decimal("0")
    try:
        if settings.get("finance.tax_enabled", scope_id=warehouse.branch_id):
            tax_rate = to_decimal(settings.get("finance.tax_rate",
                                               scope_id=warehouse.branch_id) or 0)
    except settings.SettingNotFoundError:
        pass

    sale = Sale(
        number=next_number(warehouse.branch_id, "SALE", prefix="INV-"),
        date=date.today(), branch_id=warehouse.branch_id, warehouse_id=warehouse.id,
        treasury_id=treasury.id if treasury else None, currency_code=cur,
        customer_id=customer.id if customer else None, payment_type=payment_type,
        funder_id=funder.id if funder else None,
        shift_id=shift.id if shift else None, below_min_used=allow_below_min,
        created_by_id=user_id)
    db.session.add(sale)
    db.session.flush()

    subtotal = Decimal("0")
    total_cost = Decimal("0")
    for product, qty, price, serials in norm:
        line_total = quantize_amount(qty * price)
        cogs = inv.issue_stock(product=product, warehouse=warehouse, qty=qty,
                               user_id=user_id, doc_type="sale", doc_id=sale.id,
                               serials=serials)
        unit_cost = quantize_amount(cogs / qty) if qty else Decimal("0")
        db.session.add(SaleLine(sale_id=sale.id, product_id=product.id, qty=qty,
                                unit_price=price, line_total=line_total,
                                unit_cost=unit_cost, cogs=cogs))
        subtotal += line_total
        total_cost += cogs

    subtotal = quantize_amount(subtotal)
    disc = quantize_amount(discount or 0)
    if disc < 0 or disc > subtotal:
        db.session.rollback()
        raise ValueError("قيمة الخصم غير صحيحة.")
    net = quantize_amount(subtotal - disc)  # after discount (spec SAL-01.4)
    tax = quantize_amount(net * tax_rate / 100)
    total = quantize_amount(net + tax)
    sale.subtotal, sale.tax_amount, sale.total = net, tax, total
    sale.total_cost = quantize_amount(total_cost)

    # credit-limit check (§10.3)
    if (payment_type in ("credit", "installment") and not allow_over_limit
            and customer.credit_limit > 0):
        projected = ar_balance(customer) + total
        if projected > customer.credit_limit:
            db.session.rollback()
            raise CreditLimitError(
                f"يتجاوز حد ائتمان العميل ({customer.credit_limit}).")

    # money-in side: one destination, or several tenders for a split payment
    if payment_type == "split":
        paid = quantize_amount(sum((to_decimal(p[1]) for p in payments), Decimal("0")))
        if paid != total:
            db.session.rollback()
            raise ValueError(f"مجموع المدفوع ({paid}) لا يساوي الإجمالي ({total}).")
        money_lines = []
        for method, amount, account_id in payments:
            amount = quantize_amount(amount)
            if amount <= 0:
                continue
            money_lines.append(posting.line(account_id, currency=cur, amount=amount,
                                            side="debit", fx_rate=rate))
            db.session.add(SalePayment(sale_id=sale.id, method=method,
                                       amount=amount, account_id=account_id))
    else:
        money_lines = [posting.line(dest_account, currency=cur, amount=total,
                                    side="debit", fx_rate=rate)]

    entry_lines = money_lines + [
        posting.line(posting.account_for("sales.revenue"), currency=cur,
                     amount=net, side="credit", fx_rate=rate),
    ]
    if tax > 0:
        entry_lines.append(posting.line(posting.account_for("vat.output"),
                                        currency=cur, amount=tax, side="credit", fx_rate=rate))
    entry_lines += [
        posting.line(posting.account_for("cogs"), currency=BOOK,
                     amount=sale.total_cost, side="debit", fx_rate=1),
        posting.line(posting.account_for("inventory"), currency=BOOK,
                     amount=sale.total_cost, side="credit", fx_rate=1),
    ]
    e = posting.build_entry(
        entry_date=sale.date, branch_id=warehouse.branch_id,
        source_doc_type=f"sale.{payment_type}", source_doc_id=sale.id,
        memo=f"بيع {sale.number}", user_id=user_id, lines=entry_lines)
    posting.post_entry(e, user_id=user_id)
    sale.journal_entry_id = e.id
    db.session.flush()

    if payment_type == "installment" and installment:
        _build_installments(sale, customer, installment, user_id)

    if allow_below_min:
        audit.record(action="sale.below_min", entity="sale", entity_id=sale.id,
                     new={"number": sale.number}, branch_id=warehouse.branch_id)
    audit.record(action=f"sale.{payment_type}", entity="sale", entity_id=sale.id,
                 new={"number": sale.number, "total": str(total)},
                 branch_id=warehouse.branch_id)
    return sale


def _build_installments(sale, customer, cfg, user_id):
    """Generate the installment schedule and collect any down payment (§10.5)."""
    count = int(cfg.get("count") or 1)
    down = quantize_amount(cfg.get("down_payment") or 0)
    first_due = cfg.get("first_due") or sale.date
    plan = InstallmentPlan(sale_id=sale.id, customer_id=customer.id, count=count,
                           down_payment=down, created_by_id=user_id)
    db.session.add(plan)
    db.session.flush()
    financed = quantize_amount(sale.total - down)
    per = quantize_amount(financed / count) if count else financed
    running = Decimal("0")
    for i in range(count):
        amt = per if i < count - 1 else quantize_amount(financed - running)
        running += amt
        plan.dues.append(InstallmentDue(seq=i + 1, due_date=_add_months(first_due, i),
                                        amount=amt, paid_amount=0))
    db.session.flush()
    # down payment collected immediately into a treasury, if given
    down_treasury = cfg.get("down_treasury")
    if down > 0 and down_treasury is not None:
        collect(customer=customer, to_account_id=down_treasury.account_id,
                amount=down, branch_id=sale.branch_id, user_id=user_id)


def _add_months(d, months):
    """Add whole months to a date without external deps."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


# --- BNPL funder settlement (spec §10 BNPL) -------------------------------
def create_funder(*, name_ar, branch_id, currency_code="AED", commission_pct=0,
                  fixed_fee=0, settlement_bank_account_id=None, user_id=None):
    acc = _child_account("1202", f"جهة تمويل: {name_ar}", user_id)
    f = Funder(name_ar=name_ar, branch_id=branch_id, currency_code=currency_code,
               commission_pct=quantize_amount(commission_pct),
               fixed_fee=quantize_amount(fixed_fee), account_id=acc.id,
               settlement_bank_account_id=settlement_bank_account_id,
               created_by_id=user_id)
    db.session.add(f)
    db.session.flush()
    audit.record(action="funder.create", entity="funder", entity_id=f.id,
                 new={"name": name_ar})
    return f


def funder_open_receivable(funder) -> Decimal:
    dr, cr = db.session.execute(
        db.select(
            db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == funder.account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


class OverSettlementError(Exception):
    pass


def settle_funder(*, funder, sales, bank_account_id, received_amount, user_id=None,
                  allow_overpay=False):
    """Settle a batch of funder invoices (spec §10 BNPL).

    The gap between the invoices' total and what actually arrived is booked as
    financing commission (an expense the company bears), and the funder AR is
    cleared. Receiving MORE than owed is blocked unless explicitly allowed
    (spec §16.20)."""
    invoices_total = quantize_amount(sum((s.total for s in sales), Decimal("0")))
    received = quantize_amount(received_amount)
    if received > invoices_total and not allow_overpay:  # §16.20
        raise OverSettlementError(
            f"المبلغ المستلم ({received}) أكبر من المستحق ({invoices_total}). "
            "سجّل الفرق بسبب واضح وبموافقة.")
    commission = quantize_amount(invoices_total - received)
    cur = funder.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)

    lines = [
        posting.line(bank_account_id, currency=cur, amount=received, side="debit", fx_rate=rate),
    ]
    if commission != 0:
        lines.append(posting.line(posting.account_for("funder.commission"),
                                  currency=cur, amount=abs(commission),
                                  side="debit" if commission > 0 else "credit", fx_rate=rate))
    lines.append(posting.line(funder.account_id, currency=cur, amount=invoices_total,
                              side="credit", fx_rate=rate))

    e = posting.build_entry(
        entry_date=date.today(), branch_id=funder.branch_id,
        source_doc_type="funder.settlement", memo=f"تسوية {funder.name_ar}",
        user_id=user_id, lines=lines)
    posting.post_entry(e, user_id=user_id)
    st = FunderSettlement(number=next_number(funder.branch_id, "FSET", prefix="FSET-"),
                          funder_id=funder.id, invoices_total=invoices_total,
                          received_amount=received, commission=commission,
                          bank_account_id=bank_account_id, journal_entry_id=e.id,
                          created_by_id=user_id)
    db.session.add(st)
    for s in sales:
        s.funder_settled = True
    db.session.flush()
    audit.record(action="funder.settle", entity="funder_settlement",
                 entity_id=st.id, new={"total": str(invoices_total),
                                       "commission": str(commission)})
    return st


def overdue_installments(as_of=None):
    """Installment dues past their date with an outstanding balance (§10.5)."""
    as_of = as_of or date.today()
    rows = db.session.scalars(
        db.select(InstallmentDue).filter(InstallmentDue.due_date < as_of)).all()
    out = []
    for d in rows:
        if d.outstanding > 0:
            out.append(d)
    return out


# --- collection (spec §10.4) ----------------------------------------------
def collect(*, customer, to_account_id, amount, branch_id=None, user_id=None):
    amount = quantize_amount(amount)
    cur = customer.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=branch_id,
        source_doc_type="customer.collection", memo=f"تحصيل من {customer.name_ar}",
        user_id=user_id,
        lines=[
            posting.line(to_account_id, currency=cur, amount=amount, side="debit", fx_rate=rate),
            posting.line(customer.account_id, currency=cur, amount=amount,
                         side="credit", fx_rate=rate)])
    posting.post_entry(e, user_id=user_id)
    col = Collection(number=next_number(branch_id, "COLL", prefix="RCV-"),
                     customer_id=customer.id, amount=amount, to_account_id=to_account_id,
                     journal_entry_id=e.id, created_by_id=user_id)
    db.session.add(col)
    db.session.flush()
    _apply_to_installments(customer, amount)  # oldest-first (§10.5)
    audit.record(action="customer.collect", entity="collection", entity_id=col.id,
                 new={"amount": str(amount)})
    return col


def _apply_to_installments(customer, amount):
    """Spread a collection across the customer's unpaid installment dues."""
    remaining = quantize_amount(amount)
    dues = db.session.scalars(
        db.select(InstallmentDue)
        .join(InstallmentPlan, InstallmentPlan.id == InstallmentDue.plan_id)
        .filter(InstallmentPlan.customer_id == customer.id)
        .order_by(InstallmentDue.due_date, InstallmentDue.seq)).all()
    for due in dues:
        if remaining <= 0:
            break
        out = due.outstanding
        if out <= 0:
            continue
        pay = min(out, remaining)
        due.paid_amount = quantize_amount(due.paid_amount + pay)
        remaining = quantize_amount(remaining - pay)
    db.session.flush()


# --- sales return (spec §10.7, §16.4) -------------------------------------
def returned_qty(sale_line):
    """How much of a sale line has already been returned."""
    from app.inventory.models import SaleLine
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(SalesReturnLine.qty), 0))
        .join(SalesReturn, SalesReturn.id == SalesReturnLine.return_id)
        .filter(SalesReturn.original_sale_id == sale_line.sale_id,
                SalesReturnLine.product_id == sale_line.product_id))
    return quantize_amount(total)


def sales_return(*, sale, line_qtys=None, user_id=None):
    """Return a sale — fully, or specific line quantities (§10.7, §16.4).

    line_qtys: {sale_line_id: qty}. None = full return of everything remaining.
    Returns are at the ORIGINAL cost and price, and cannot exceed what was sold.
    """
    wh = sale.warehouse
    rate = fx.rate_to_book(sale.currency_code) or quantize_rate(1)

    # decide quantities per line
    plan = []
    for l in sale.lines:
        already = returned_qty(l)
        remaining = quantize_amount(l.qty - already)
        if line_qtys is None:
            qty = remaining
        else:
            qty = quantize_amount(line_qtys.get(l.id, 0))
        if qty <= 0:
            continue
        if qty > remaining:
            raise AlreadyReturnedError(
                f"الكمية المرتجعة لـ {l.product.name_ar} تتجاوز المباع المتبقي.")
        plan.append((l, qty))
    if not plan:
        raise AlreadyReturnedError("لا توجد كمية قابلة للرد.")

    ret_total = quantize_amount(sum((q * l.unit_price for l, q in plan), Decimal("0")))
    ret_cost = quantize_amount(sum((q * l.unit_cost for l, q in plan), Decimal("0")))
    # scale tax proportionally to the returned value
    ret_tax = quantize_amount(
        (sale.tax_amount * ret_total / sale.subtotal) if sale.subtotal else 0)

    ret = SalesReturn(number=next_number(sale.branch_id, "SRET", prefix="RET-"),
                      original_sale_id=sale.id, branch_id=sale.branch_id,
                      total=quantize_amount(ret_total + ret_tax), total_cost=ret_cost,
                      created_by_id=user_id)
    db.session.add(ret)
    db.session.flush()

    for l, qty in plan:
        db.session.add(SalesReturnLine(return_id=ret.id, product_id=l.product_id,
                                       qty=qty, unit_price=l.unit_price,
                                       unit_cost=l.unit_cost))
        inv.add_stock_batch(product=l.product, warehouse=wh, qty=qty,
                            unit_cost=quantize_amount(l.unit_cost / (rate or 1)),
                            user_id=user_id, source=f"return:{ret.id}")

    ret_subtotal = ret_total

    # money side depends on how the sale was paid
    if sale.payment_type == "credit" and sale.customer:
        money_account = sale.customer.account_id  # reduce AR (credit)
    elif sale.payment_type == "cash" and sale.treasury:
        money_account = sale.treasury.account_id
    else:
        money_account = posting.account_for("cash.main")

    lines = [
        posting.line(posting.account_for("sales.revenue"), currency=sale.currency_code,
                     amount=ret_subtotal, side="debit", fx_rate=rate),
    ]
    if ret_tax and ret_tax > 0:
        lines.append(posting.line(posting.account_for("vat.output"),
                                  currency=sale.currency_code, amount=ret_tax,
                                  side="debit", fx_rate=rate))
    lines += [
        posting.line(money_account, currency=sale.currency_code, amount=ret.total,
                     side="credit", fx_rate=rate),
        posting.line(posting.account_for("inventory"), currency=BOOK,
                     amount=ret_cost, side="debit", fx_rate=1),
        posting.line(posting.account_for("cogs"), currency=BOOK,
                     amount=ret_cost, side="credit", fx_rate=1),
    ]
    e = posting.build_entry(
        entry_date=date.today(), branch_id=sale.branch_id,
        source_doc_type="sale.return", source_doc_id=ret.id,
        memo=f"مرتجع مبيعات للفاتورة {sale.number}", user_id=user_id, lines=lines)
    posting.post_entry(e, user_id=user_id)
    ret.journal_entry_id = e.id
    # mark fully-returned only when every line is exhausted
    if all(returned_qty(l) >= l.qty for l in sale.lines):
        sale.returned = True
    db.session.flush()
    audit.record(action="sale.return", entity="sales_return", entity_id=ret.id,
                 new={"sale": sale.number}, branch_id=sale.branch_id)
    return ret


def make_exchange(*, original_sale, return_line_qtys, new_lines, warehouse,
                  shift=None, treasury=None, bank_account=None, customer=None,
                  payment_type="cash", user_id=None, allow_below_min=False):
    """Exchange: return selected lines of an existing sale and sell replacement
    items in one operation (spec POS — استبدال).

    The return restocks the goods and reverses their revenue/COGS; the new sale
    is a normal sale. The cashier settles only the *difference*: if the new items
    cost more the customer pays it, if less the customer is refunded — this nets
    out naturally in the drawer/AR because both documents post on their own.

    Returns {"return": SalesReturn, "sale": Sale, "difference": Decimal}. A
    positive difference means the customer owes money; negative means a refund.
    """
    if not return_line_qtys or not any(
            to_decimal(v) > 0 for v in return_line_qtys.values()):
        raise ValueError("حدد الأصناف المُرتجَعة للاستبدال.")
    if not new_lines:
        raise inv.EmptySaleError("أضف الأصناف البديلة — أو استخدم المرتجع العادي.")

    ret = sales_return(sale=original_sale, line_qtys=return_line_qtys, user_id=user_id)
    new_sale = make_sale(
        warehouse=warehouse, lines=new_lines, payment_type=payment_type,
        treasury=treasury, bank_account=bank_account, customer=customer,
        shift=shift, user_id=user_id, allow_below_min=allow_below_min)
    diff = quantize_amount(new_sale.total - ret.total)
    audit.record(action="sale.exchange", entity="sale", entity_id=new_sale.id,
                 new={"original": original_sale.number, "return": ret.number,
                      "new_sale": new_sale.number, "difference": str(diff)},
                 branch_id=warehouse.branch_id)
    return {"return": ret, "sale": new_sale, "difference": diff}


# --- quotes (spec SAL-08) -------------------------------------------------
def create_quote(*, customer, warehouse, lines, valid_until=None, user_id=None):
    from app.sales.models import Quote, QuoteLine
    q = Quote(number=next_number(warehouse.branch_id, "QT", prefix="QT-"),
              customer_id=customer.id if customer else None,
              warehouse_id=warehouse.id, valid_until=valid_until, status="draft",
              created_by_id=user_id)
    db.session.add(q)
    db.session.flush()
    for product, qty, price in lines:
        q.lines.append(QuoteLine(product_id=product.id, qty=quantize_amount(qty),
                                 unit_price=quantize_amount(price)))
    db.session.flush()
    audit.record(action="quote.create", entity="quote", entity_id=q.id,
                 new={"number": q.number})
    return q


def convert_quote_to_sale(quote, *, payment_type="credit", treasury=None,
                          bank_account=None, shift=None, user_id=None,
                          allow_below_min=False):
    """Convert an accepted quote to a sale, re-checking min price + stock (SAL-08.3)."""
    from app.inventory.models import Product
    if quote.sale_id:
        raise ValueError("سبق تحويل هذا العرض.")
    lines = [(db.session.get(Product, l.product_id), l.qty, l.unit_price)
             for l in quote.lines]
    sale = make_sale(warehouse=quote.warehouse, lines=lines,
                     payment_type=payment_type, treasury=treasury,
                     bank_account=bank_account, customer=quote.customer, shift=shift,
                     user_id=user_id, allow_below_min=allow_below_min)
    quote.status = "accepted"
    quote.sale_id = sale.id
    db.session.flush()
    audit.record(action="quote.convert", entity="quote", entity_id=quote.id,
                 new={"sale": sale.number})
    return sale
