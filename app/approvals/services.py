"""Unified approvals inbox.

Several workflows pause in a *pending* state until an authorized user (usually
the owner) approves them: over-limit treasury transfers, large custody expenses,
and manual journal entries awaiting a second person to post them (separation of
duties). This module gathers them into one queue so nothing waits unseen — the
"صندوق الاعتمادات" the owner checks each day.
"""
from __future__ import annotations

from app.accounting.models import JournalEntry
from app.custody.models import Custody, CustodyTxn
from app.extensions import db
from app.treasury.models import TreasuryTxn


def pending_treasury_transfers():
    return db.session.scalars(
        db.select(TreasuryTxn).filter_by(status="pending_approval")
        .order_by(TreasuryTxn.id)).all()


def pending_custody_expenses():
    rows = db.session.scalars(
        db.select(CustodyTxn).filter_by(kind="expense", status="pending")
        .order_by(CustodyTxn.id)).all()
    out = []
    for t in rows:
        out.append({"txn": t, "custody": db.session.get(Custody, t.custody_id)})
    return out


def pending_manual_entries():
    """Draft manual journal entries awaiting posting by another user."""
    return db.session.scalars(
        db.select(JournalEntry)
        .filter(JournalEntry.status == "draft",
                JournalEntry.source_doc_type == "manual")
        .order_by(JournalEntry.id)).all()


def summary():
    transfers = pending_treasury_transfers()
    custody = pending_custody_expenses()
    entries = pending_manual_entries()
    return {
        "transfers": transfers,
        "custody": custody,
        "entries": entries,
        "count": len(transfers) + len(custody) + len(entries),
    }


def pending_count() -> int:
    """Cheap count for the sidebar badge (no object hydration needed)."""
    n = 0
    n += db.session.scalar(db.select(db.func.count(TreasuryTxn.id))
                           .filter_by(status="pending_approval")) or 0
    n += db.session.scalar(db.select(db.func.count(CustodyTxn.id))
                           .filter_by(kind="expense", status="pending")) or 0
    n += db.session.scalar(
        db.select(db.func.count(JournalEntry.id))
        .filter(JournalEntry.status == "draft",
                JournalEntry.source_doc_type == "manual")) or 0
    return int(n)
