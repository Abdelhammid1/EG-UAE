"""Consignments / العهد (spec §12).

A custody is a rep's cash advance held in a dedicated GL account (child of عهد
المناديب). Money is issued from a treasury, spent on purchases/expenses, and
settled/closed. The available balance is the account balance — never overspent.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount
from app.extensions import db


class Custody(TimestampMixin, db.Model):
    __tablename__ = "custody"

    name_ar: Mapped[str] = mapped_column(db.String(160))  # rep name
    rep_user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))
    status: Mapped[str] = mapped_column(db.String(10), default="open")

    branch = db.relationship("Branch")
    account = db.relationship("Account")
    rep = db.relationship("User", foreign_keys=[rep_user_id])


class CustodyTxn(TimestampMixin, db.Model):
    __tablename__ = "custody_txn"

    custody_id: Mapped[int] = mapped_column(db.ForeignKey("custody.id"))
    date: Mapped[date] = mapped_column(default=date.today)
    kind: Mapped[str] = mapped_column(db.String(12))  # issue/purchase/expense/return
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    memo: Mapped[str | None] = mapped_column(db.String(255))
    ref_doc: Mapped[str | None] = mapped_column(db.String(40))
    # per-expense approval (CUS-04): posted / pending / rejected
    status: Mapped[str] = mapped_column(db.String(10), default="posted")
    expense_account_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))
    approved_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    custody = db.relationship("Custody")

    @property
    def kind_label(self):
        return {"issue": "صرف عهدة", "purchase": "شراء", "expense": "مصروف",
                "return": "إرجاع"}.get(self.kind, self.kind)

    @property
    def status_label(self):
        return {"posted": "مُرحَّل", "pending": "بانتظار الاعتماد",
                "rejected": "مرفوض"}.get(self.status, self.status)
