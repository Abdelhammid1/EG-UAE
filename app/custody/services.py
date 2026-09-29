"""Consignment / العهد services (spec §12)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.accounting import posting
from app.accounting.models import Account, JournalEntry, JournalLine
from app.core import audit, fx, settings
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.custody.models import Custody, CustodyTxn
from app.extensions import db


class OverspendError(Exception):
    pass


class CustodyClosedError(Exception):
    pass


class ApprovalError(Exception):
    pass


def _child_account(parent_code, name_ar, user_id=None):
    parent = db.session.scalar(db.select(Account).filter_by(code=parent_code))
    count = db.session.scalar(
        db.select(db.func.count(Account.id))
        .filter(Account.code.like(f"{parent_code}-%")))
    acc = Account(code=f"{parent_code}-{(count or 0) + 1:03d}", name_ar=name_ar,
                  name_en=name_ar, type="asset", is_postable=True,
                  parent_id=parent.id if parent else None, created_by_id=user_id)
    db.session.add(acc)
    db.session.flush()
    return acc


def create_custody(*, name_ar, branch_id, currency_code="AED", rep_user_id=None,
                   user_id=None):
    acc = _child_account("1203", f"عهدة: {name_ar}", user_id)
    c = Custody(name_ar=name_ar, branch_id=branch_id, currency_code=currency_code,
                rep_user_id=rep_user_id, account_id=acc.id, status="open",
                created_by_id=user_id)
    db.session.add(c)
    db.session.flush()
    audit.record(action="custody.create", entity="custody", entity_id=c.id,
                 new={"name": name_ar})
    return c


def available(custody: Custody) -> Decimal:
    """Available balance = issued − spent (debit-normal), in custody currency."""
    dr, cr = db.session.execute(
        db.select(
            db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == custody.account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


def issue(*, custody, from_treasury, amount, memo=None, user_id=None):
    """Issue cash from a treasury to the rep's custody (spec §12.1)."""
    if custody.status != "open":
        raise CustodyClosedError("العهدة مغلقة.")
    if from_treasury.currency_code != custody.currency_code:
        raise ValueError("عملة الخزينة يجب أن تطابق عملة العهدة.")
    amount = quantize_amount(amount)
    cur = custody.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=custody.branch_id,
        source_doc_type="custody.issue", source_doc_id=custody.id,
        memo=memo or f"صرف عهدة — {custody.name_ar}", user_id=user_id,
        lines=[
            posting.line(custody.account_id, currency=cur, amount=amount,
                         side="debit", fx_rate=rate),
            posting.line(from_treasury.account_id, currency=cur, amount=amount,
                         side="credit", fx_rate=rate),
        ])
    posting.post_entry(e, user_id=user_id)
    txn = CustodyTxn(custody_id=custody.id, date=e.date, kind="issue",
                     amount=amount, memo=memo, journal_entry_id=e.id,
                     created_by_id=user_id)
    db.session.add(txn)
    db.session.flush()
    audit.record(action="custody.issue", entity="custody", entity_id=custody.id,
                 new={"amount": str(amount)})
    return txn


def pending_expenses(custody) -> Decimal:
    """Sum of custody expenses awaiting owner approval (not yet posted)."""
    total = db.session.scalar(
        db.select(db.func.coalesce(db.func.sum(CustodyTxn.amount), 0))
        .filter(CustodyTxn.custody_id == custody.id,
                CustodyTxn.kind == "expense", CustodyTxn.status == "pending"))
    return quantize_amount(to_decimal(total))


def available_to_spend(custody) -> Decimal:
    """Available minus amounts already reserved by pending (unapproved) expenses."""
    return quantize_amount(available(custody) - pending_expenses(custody))


def _expense_approval_limit() -> Decimal:
    """Threshold above which a custody expense needs owner approval (0 = none)."""
    try:
        return to_decimal(settings.get("custody.expense_approval_limit") or 0)
    except settings.SettingNotFoundError:
        return Decimal("0")


def spend_expense(*, custody, expense_account_id, amount, memo=None, user_id=None):
    """Record an expense paid from custody (spec §12.2).

    If the amount exceeds the configured approval limit (CUS-04) it is held as a
    *pending* transaction with no journal impact until the owner approves it; the
    reserved amount still counts against the available balance so it cannot be
    double-spent.
    """
    if custody.status != "open":
        raise CustodyClosedError("العهدة مغلقة.")
    amount = quantize_amount(amount)
    if amount > available_to_spend(custody):  # §12.3
        raise OverspendError(
            f"المبلغ {amount} يتجاوز رصيد العهدة المتاح {available_to_spend(custody)}.")

    limit = _expense_approval_limit()
    if limit > 0 and amount > limit:
        txn = CustodyTxn(custody_id=custody.id, date=date.today(), kind="expense",
                         amount=amount, memo=memo, status="pending",
                         expense_account_id=expense_account_id, created_by_id=user_id)
        db.session.add(txn)
        db.session.flush()
        audit.record(action="custody.expense.pending", entity="custody",
                     entity_id=custody.id, new={"amount": str(amount),
                                                 "txn_id": txn.id})
        return txn

    txn = _post_expense(custody, expense_account_id, amount, memo, user_id)
    audit.record(action="custody.expense", entity="custody", entity_id=custody.id,
                 new={"amount": str(amount)})
    return txn


def _post_expense(custody, expense_account_id, amount, memo, user_id):
    cur = custody.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=custody.branch_id,
        source_doc_type="custody.expense", source_doc_id=custody.id,
        memo=memo or "مصروف من عهدة", user_id=user_id,
        lines=[
            posting.line(expense_account_id, currency=cur, amount=amount,
                         side="debit", fx_rate=rate),
            posting.line(custody.account_id, currency=cur, amount=amount,
                         side="credit", fx_rate=rate),
        ])
    posting.post_entry(e, user_id=user_id)
    txn = CustodyTxn(custody_id=custody.id, date=e.date, kind="expense",
                     amount=amount, memo=memo, status="posted",
                     expense_account_id=expense_account_id,
                     journal_entry_id=e.id, created_by_id=user_id)
    db.session.add(txn)
    db.session.flush()
    return txn


def approve_expense(txn: CustodyTxn, *, user_id=None):
    """Owner approves a pending custody expense — posts its journal entry.

    Separation of duties: the approver may not be the person who recorded it
    (spec CUS-04 / §12)."""
    if txn.status != "pending":
        raise ApprovalError("المصروف ليس بانتظار الاعتماد.")
    if txn.created_by_id and txn.created_by_id == user_id:
        raise ApprovalError("لا يجوز اعتماد مصروف سجّلته بنفسك — يعتمده المالك.")
    custody = db.session.get(Custody, txn.custody_id)
    if custody.status != "open":
        raise CustodyClosedError("العهدة مغلقة.")
    if txn.amount > available(custody):
        raise OverspendError("لم يعد الرصيد كافيًا لاعتماد هذا المصروف.")
    cur = custody.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    e = posting.build_entry(
        entry_date=date.today(), branch_id=custody.branch_id,
        source_doc_type="custody.expense", source_doc_id=custody.id,
        memo=txn.memo or "مصروف من عهدة", user_id=user_id,
        lines=[
            posting.line(txn.expense_account_id, currency=cur, amount=txn.amount,
                         side="debit", fx_rate=rate),
            posting.line(custody.account_id, currency=cur, amount=txn.amount,
                         side="credit", fx_rate=rate),
        ])
    posting.post_entry(e, user_id=user_id)
    txn.status = "posted"
    txn.journal_entry_id = e.id
    txn.approved_by_id = user_id
    db.session.flush()
    audit.record(action="custody.expense.approve", entity="custody",
                 entity_id=custody.id, new={"amount": str(txn.amount),
                                            "txn_id": txn.id})
    return txn


def reject_expense(txn: CustodyTxn, *, user_id=None):
    """Owner rejects a pending custody expense — it is voided, no journal impact."""
    if txn.status != "pending":
        raise ApprovalError("المصروف ليس بانتظار الاعتماد.")
    txn.status = "rejected"
    txn.approved_by_id = user_id
    db.session.flush()
    audit.record(action="custody.expense.reject", entity="custody",
                 entity_id=txn.custody_id, new={"txn_id": txn.id})
    return txn


def pending_expense_txns():
    """All custody expenses awaiting approval (for the approvals inbox)."""
    return db.session.scalars(
        db.select(CustodyTxn).filter_by(kind="expense", status="pending")
        .order_by(CustodyTxn.id)).all()


def close(*, custody, to_treasury, user_id=None):
    """Return the remaining balance to a treasury and close (spec §12.5)."""
    if custody.status != "open":
        raise CustodyClosedError("العهدة مغلقة بالفعل.")
    bal = available(custody)
    cur = custody.currency_code
    rate = fx.rate_to_book(cur) or quantize_rate(1)
    if bal > 0:
        e = posting.build_entry(
            entry_date=date.today(), branch_id=custody.branch_id,
            source_doc_type="custody.return", source_doc_id=custody.id,
            memo=f"إغلاق عهدة — {custody.name_ar}", user_id=user_id,
            lines=[
                posting.line(to_treasury.account_id, currency=cur, amount=bal,
                             side="debit", fx_rate=rate),
                posting.line(custody.account_id, currency=cur, amount=bal,
                             side="credit", fx_rate=rate),
            ])
        posting.post_entry(e, user_id=user_id)
        db.session.add(CustodyTxn(custody_id=custody.id, date=e.date, kind="return",
                                  amount=bal, journal_entry_id=e.id,
                                  created_by_id=user_id))
    custody.status = "closed"
    db.session.flush()
    audit.record(action="custody.close", entity="custody", entity_id=custody.id,
                 new={"returned": str(bal)})
    return custody


def open_custodies():
    return db.session.scalars(db.select(Custody).filter_by(status="open")).all()


def departed_rep_alerts():
    """Open custodies whose rep is suspended — must be settled (spec §12.5)."""
    out = []
    for c in open_custodies():
        if c.rep and not c.rep.is_active:
            out.append({"custody": c, "available": available(c)})
    return out
