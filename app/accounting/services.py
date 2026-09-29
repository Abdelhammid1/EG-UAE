"""Reporting and period services for the accounting kernel."""
from __future__ import annotations

from datetime import datetime

from app.accounting.models import Account, AccountingPeriod, JournalEntry, JournalLine
from app.core import audit
from app.core.money import quantize_amount
from app.extensions import db


class OwnerApprovalRequired(Exception):
    """Closing/reopening a period needs the owner's approval (spec §13.6)."""


def trial_balance(branch_id: int | None = None, up_to=None):
    """Sum posted movements per account in book currency (spec §14 ميزان المراجعة).

    Returns rows [{account, debit, credit, balance, side}] and totals.
    """
    q = (
        db.select(
            Account,
            db.func.coalesce(db.func.sum(JournalLine.debit_book), 0),
            db.func.coalesce(db.func.sum(JournalLine.credit_book), 0),
        )
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalEntry.status == "posted")
        .group_by(Account.id)
        .order_by(Account.code)
    )
    if branch_id:
        q = q.filter(JournalEntry.branch_id == branch_id)
    if up_to:
        q = q.filter(JournalEntry.date <= up_to)

    rows, total_debit, total_credit = [], quantize_amount(0), quantize_amount(0)
    for account, dr, cr in db.session.execute(q).all():
        # DecimalText returns Decimal; sums on SQLite may come back as str/num.
        dr, cr = quantize_amount(dr), quantize_amount(cr)
        net = dr - cr
        rows.append({
            "account": account,
            "debit": dr,
            "credit": cr,
            "balance": abs(net),
            "side": "debit" if net >= 0 else "credit",
        })
        total_debit += dr
        total_credit += cr
    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }


def close_period(period: AccountingPeriod, *, user_id, approved_by_id):
    """Close a period. Requires an explicit owner approver (spec §13.6)."""
    if approved_by_id is None:
        raise OwnerApprovalRequired("إقفال الفترة يتطلب اعتماد المالك.")
    period.status = "closed"
    period.closed_by_id = user_id
    period.approved_by_id = approved_by_id
    period.closed_at = datetime.utcnow()
    audit.record(
        action="period.close",
        entity="accounting_period",
        entity_id=period.id,
        new={"name": period.name, "approved_by": approved_by_id},
    )


def reopen_period(period: AccountingPeriod, *, user_id, approved_by_id):
    """Reopen a closed period. Requires owner approval and is audited (§13.6)."""
    if approved_by_id is None:
        raise OwnerApprovalRequired("إعادة فتح الفترة تتطلب اعتماد المالك.")
    period.status = "open"
    period.approved_by_id = approved_by_id
    audit.record(
        action="period.reopen",
        entity="accounting_period",
        entity_id=period.id,
        old={"status": "closed"},
        new={"status": "open", "approved_by": approved_by_id},
    )
