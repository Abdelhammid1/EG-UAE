"""Reporting engine (spec §14). Everything reads from the posted ledger."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.accounting.models import (
    Account, AccountingPeriod, JournalEntry, JournalLine,
)
from app.accounting.models import DEBIT_NORMAL
from app.core import fx
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.extensions import db

BOOK = "AED"


def ledger_totals(*, start=None, end=None, branch_id=None):
    """Per-account (debit_book, credit_book) over a window, posted only."""
    q = (db.select(Account.id, Account,
                   db.func.coalesce(db.func.sum(JournalLine.debit_book), 0),
                   db.func.coalesce(db.func.sum(JournalLine.credit_book), 0))
         .join(JournalLine, JournalLine.account_id == Account.id)
         .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
         .filter(JournalEntry.status == "posted")
         .group_by(Account.id))
    if start:
        q = q.filter(JournalEntry.date >= start)
    if end:
        q = q.filter(JournalEntry.date <= end)
    if branch_id:
        q = q.filter(JournalEntry.branch_id == branch_id)
    out = {}
    for aid, acc, dr, cr in db.session.execute(q).all():
        out[aid] = (acc, quantize_amount(dr), quantize_amount(cr))
    return out


def _net(acc, dr, cr):
    """Signed balance in the account's normal direction (positive = normal)."""
    return (dr - cr) if acc.type in DEBIT_NORMAL else (cr - dr)


# --- income statement (spec §14 قائمة الدخل) ------------------------------
def income_statement(*, start, end, branch_id=None):
    from app.accounting.posting import account_for, MappingMissingError
    try:
        cogs_id = account_for("cogs")
    except MappingMissingError:
        cogs_id = None
    totals = ledger_totals(start=start, end=end, branch_id=branch_id)
    revenue, cogs, expenses = [], Decimal("0"), []
    revenue_total = Decimal("0")
    expense_total = Decimal("0")
    for aid, (acc, dr, cr) in totals.items():
        if acc.type == "revenue":
            amt = quantize_amount(cr - dr)
            if amt:
                revenue.append({"account": acc, "amount": amt})
                revenue_total += amt
        elif acc.type == "expense":
            amt = quantize_amount(dr - cr)
            if aid == cogs_id:
                cogs += amt
            elif amt:
                expenses.append({"account": acc, "amount": amt})
                expense_total += amt
    revenue_total = quantize_amount(revenue_total)
    cogs = quantize_amount(cogs)
    gross = quantize_amount(revenue_total - cogs)
    net = quantize_amount(gross - expense_total)
    return {"revenue": revenue, "revenue_total": revenue_total, "cogs": cogs,
            "gross": gross, "expenses": expenses, "expense_total": expense_total,
            "net": net}


# --- balance sheet (spec §14 الميزانية العمومية) --------------------------
def balance_sheet(*, as_of, branch_id=None):
    totals = ledger_totals(end=as_of, branch_id=branch_id)
    assets, liabilities, equity = [], [], []
    a_tot = l_tot = e_tot = Decimal("0")
    net_income = Decimal("0")
    for aid, (acc, dr, cr) in totals.items():
        bal = quantize_amount(_net(acc, dr, cr))
        if acc.type == "asset":
            if bal:
                assets.append({"account": acc, "amount": bal}); a_tot += bal
        elif acc.type == "liability":
            if bal:
                liabilities.append({"account": acc, "amount": bal}); l_tot += bal
        elif acc.type == "equity":
            if bal:
                equity.append({"account": acc, "amount": bal}); e_tot += bal
        elif acc.type == "revenue":
            net_income += quantize_amount(cr - dr)
        elif acc.type == "expense":
            net_income -= quantize_amount(dr - cr)
    net_income = quantize_amount(net_income)
    e_tot = quantize_amount(e_tot + net_income)
    return {"assets": assets, "assets_total": quantize_amount(a_tot),
            "liabilities": liabilities, "liabilities_total": quantize_amount(l_tot),
            "equity": equity, "equity_total": e_tot, "net_income": net_income,
            "balanced": quantize_amount(a_tot) == quantize_amount(l_tot + e_tot)}


# --- general ledger (كشف حساب عام) ----------------------------------------
def general_ledger(account_id, *, start=None, end=None, branch_id=None):
    q = (db.select(JournalLine, JournalEntry)
         .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
         .filter(JournalLine.account_id == account_id,
                 JournalEntry.status == "posted")
         .order_by(JournalEntry.date, JournalEntry.id))
    if start:
        q = q.filter(JournalEntry.date >= start)
    if end:
        q = q.filter(JournalEntry.date <= end)
    if branch_id:
        q = q.filter(JournalEntry.branch_id == branch_id)
    acc = db.session.get(Account, account_id)
    rows, running = [], Decimal("0")
    for line, entry in db.session.execute(q).all():
        delta = quantize_amount(to_decimal(line.debit_book) - to_decimal(line.credit_book))
        if acc and acc.type not in DEBIT_NORMAL:
            delta = -delta
        running = quantize_amount(running + delta)
        rows.append({"entry": entry, "line": line, "balance": running})
    return rows


# --- dashboard aggregates (spec §14 لوحة المالك) --------------------------
def sum_by_type(account_type, *, branch_id=None, code_prefix=None):
    totals = ledger_totals(branch_id=branch_id)
    s = Decimal("0")
    for aid, (acc, dr, cr) in totals.items():
        if acc.type != account_type:
            continue
        if code_prefix and not acc.code.startswith(code_prefix):
            continue
        s += _net(acc, dr, cr)
    return quantize_amount(s)


def dashboard(branch_id=None):
    """Cash, inventory, receivables, payables (book currency)."""
    cash = sum_by_type("asset", branch_id=branch_id, code_prefix="11")
    inventory = sum_by_type("asset", branch_id=branch_id, code_prefix="1301")
    customers_ar = sum_by_type("asset", branch_id=branch_id, code_prefix="1201")
    funders_ar = sum_by_type("asset", branch_id=branch_id, code_prefix="1202")
    payables = sum_by_type("liability", branch_id=branch_id, code_prefix="2101")
    inc = income_statement(start=date(date.today().year, 1, 1), end=date.today(),
                           branch_id=branch_id)
    return {"cash": cash, "inventory": inventory, "customers_ar": customers_ar,
            "funders_ar": funders_ar, "payables": payables,
            "net_income": inc["net"], "revenue": inc["revenue_total"]}


# --- tax report (spec §14 الضرائب) ----------------------------------------
def tax_report(*, start, end, branch_id=None):
    from app.accounting.posting import account_for, MappingMissingError
    try:
        vat_id = account_for("vat.output")
    except MappingMissingError:
        return {"output": Decimal("0"), "input": Decimal("0"), "net": Decimal("0")}
    totals = ledger_totals(start=start, end=end, branch_id=branch_id)
    acc, dr, cr = totals.get(vat_id, (None, Decimal("0"), Decimal("0")))
    output = quantize_amount(cr - dr)  # VAT collected on sales
    return {"output": output, "input": Decimal("0"), "net": output}


# --- sales report (spec §14 المبيعات) -------------------------------------
def sales_by_product(*, start=None, end=None, branch_id=None):
    from app.inventory.models import Sale, SaleLine, Product
    q = (db.select(Product, db.func.sum(SaleLine.qty),
                   db.func.sum(SaleLine.line_total), db.func.sum(SaleLine.cogs))
         .join(SaleLine, SaleLine.product_id == Product.id)
         .join(Sale, Sale.id == SaleLine.sale_id)
         .filter(Sale.returned == False)  # noqa: E712
         .group_by(Product.id))
    if start:
        q = q.filter(Sale.date >= start)
    if end:
        q = q.filter(Sale.date <= end)
    if branch_id:
        q = q.filter(Sale.branch_id == branch_id)
    rows = []
    for p, qty, revenue, cogs in db.session.execute(q).all():
        revenue = quantize_amount(revenue); cogs = quantize_amount(cogs)
        rows.append({"product": p, "qty": quantize_amount(qty), "revenue": revenue,
                     "cogs": cogs, "margin": quantize_amount(revenue - cogs)})
    rows.sort(key=lambda r: r["revenue"], reverse=True)
    return rows


# --- unrealized FX revaluation at period close (spec §5.9) ----------------
def revalue_unrealized(*, period, user_id=None):
    """Revalue foreign-currency entity balances at the closing rate and post an
    adjustment that auto-reverses at the start of the next period (§5.9)."""
    from app.accounting.posting import account_for, build_entry, post_entry
    from app.treasury.models import Treasury, BankAccount
    from app.sales.models import Customer, Funder
    from app.purchasing.models import Supplier

    entities = []
    for model in (Treasury, BankAccount, Customer, Supplier, Funder):
        for e in db.session.scalars(db.select(model)).all():
            if getattr(e, "currency_code", BOOK) != BOOK:
                entities.append(e)

    lines = []
    net = Decimal("0")
    close_date = period.end_date
    for e in entities:
        native = _account_native_balance(e.account_id)
        if native == 0:
            continue
        rate = fx.rate_to_book(e.currency_code, None) or quantize_rate(1)
        book_now = _account_book_balance(e.account_id)
        book_should = quantize_amount(native * rate)
        diff = quantize_amount(book_should - book_now)
        if diff == 0:
            continue
        # adjust the entity account to its revalued book amount
        side = "debit" if diff > 0 else "credit"
        lines.append({"account_id": e.account_id, "currency_code": BOOK,
                      "fx_rate": quantize_rate(1),
                      "debit_original": abs(diff) if diff > 0 else 0,
                      "credit_original": abs(diff) if diff < 0 else 0,
                      "debit_book": abs(diff) if diff > 0 else 0,
                      "credit_book": abs(diff) if diff < 0 else 0, "memo": None})
        net += diff

    if not lines:
        return None
    net = quantize_amount(net)
    # balancing side to unrealized gain/loss
    gain_acc = account_for("fx.gain")
    loss_acc = account_for("fx.loss")
    if net > 0:  # net asset increase -> unrealized gain (credit)
        bal = {"account_id": gain_acc, "currency_code": BOOK, "fx_rate": quantize_rate(1),
               "debit_original": 0, "credit_original": net, "debit_book": 0,
               "credit_book": net, "memo": "فروق عملة غير محققة"}
    else:
        bal = {"account_id": loss_acc, "currency_code": BOOK, "fx_rate": quantize_rate(1),
               "debit_original": -net, "credit_original": 0, "debit_book": -net,
               "credit_book": 0, "memo": "فروق عملة غير محققة"}
    lines.append(bal)

    entry = build_entry(entry_date=close_date, branch_id=None,
                        source_doc_type="fx.unrealized", source_doc_id=period.id,
                        memo=f"إعادة تقييم فروق العملة غير المحققة — {period.name}",
                        user_id=user_id, lines=lines)
    post_entry(entry, user_id=user_id)

    # auto-reversal at the start of the next period (§5.9)
    from app.accounting.posting import reverse_entry
    reverse_entry(entry, entry_date=close_date + timedelta(days=1), user_id=user_id,
                  memo=f"عكس إعادة تقييم فروق العملة — {period.name}")
    return entry


def _account_native_balance(account_id):
    dr, cr = db.session.execute(
        db.select(db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
                  db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


def _account_book_balance(account_id):
    dr, cr = db.session.execute(
        db.select(db.func.coalesce(db.func.sum(JournalLine.debit_book), 0),
                  db.func.coalesce(db.func.sum(JournalLine.credit_book), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


# --- daily close (spec POS-07) --------------------------------------------
def daily_close(*, day=None, branch_id=None):
    from app.inventory.models import Sale
    from app.sales.models import SalesReturn
    day = day or date.today()
    q = db.select(Sale).filter(Sale.date == day)
    if branch_id:
        q = q.filter(Sale.branch_id == branch_id)
    sales = db.session.scalars(q).all()
    by_method = {}
    total = Decimal("0")
    for s in sales:
        by_method[s.payment_type] = quantize_amount(
            by_method.get(s.payment_type, Decimal("0")) + s.total)
        total += s.total
    rq = db.select(SalesReturn).filter(SalesReturn.date == day)
    if branch_id:
        rq = rq.filter(SalesReturn.branch_id == branch_id)
    returns = db.session.scalars(rq).all()
    returns_total = quantize_amount(sum((r.total for r in returns), Decimal("0")))
    return {"day": day, "count": len(sales), "total": quantize_amount(total),
            "by_method": by_method, "returns_count": len(returns),
            "returns_total": returns_total}


# --- exception reports (spec REP-05) --------------------------------------
def exceptions():
    from app.core.audit import AuditLog
    from app.sales.models import Shift, SalesReturn
    below_min = db.session.scalars(
        db.select(AuditLog).filter_by(action="sale.below_min")
        .order_by(AuditLog.at.desc()).limit(200)).all()
    price_changes = db.session.scalars(
        db.select(AuditLog).filter(AuditLog.action.in_(
            ["product.price.edit", "setting.change"]))
        .order_by(AuditLog.at.desc()).limit(200)).all()
    over_short = db.session.scalars(
        db.select(Shift).filter(Shift.status == "closed", Shift.over_short != 0)
        .order_by(Shift.id.desc()).limit(200)).all()
    returns = db.session.scalars(
        db.select(SalesReturn).order_by(SalesReturn.id.desc()).limit(200)).all()
    return {"below_min": below_min, "price_changes": price_changes,
            "over_short": over_short, "returns": returns}
