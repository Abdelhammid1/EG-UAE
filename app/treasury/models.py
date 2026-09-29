"""Treasuries and bank accounts (spec §6).

Each cash entity owns a dedicated GL account, so its balance is derived purely
from posted journal lines — no balance ever lives outside the ledger (§1.1).
Currency is fixed at creation and locked after the first movement (§16.18).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Rate
from app.extensions import db

TREASURY_TYPES = {
    "main": "رئيسية",
    "drawer": "درج كاشير",
    "wallet": "محفظة إلكترونية",
    "petty": "مصروفات صغيرة",
}


class Treasury(TimestampMixin, db.Model):
    """خزينة — cash box (spec §4, §6)."""

    __tablename__ = "treasury"

    name_ar: Mapped[str] = mapped_column(db.String(120))
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"), index=True)
    currency_code: Mapped[str] = mapped_column(db.String(3))
    type: Mapped[str] = mapped_column(db.String(20), default="main")
    opening_balance: Mapped[Decimal] = mapped_column(Amount(), default=0)
    # dedicated GL account
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    branch = db.relationship("Branch")
    account = db.relationship("Account")

    @property
    def type_label(self):
        return TREASURY_TYPES.get(self.type, self.type)

    @property
    def kind(self):
        return "treasury"


class BankAccount(TimestampMixin, db.Model):
    """حساب بنكي — bank account (spec §4, §6)."""

    __tablename__ = "bank_account"

    name_ar: Mapped[str] = mapped_column(db.String(120))
    bank_name: Mapped[str] = mapped_column(db.String(120), default="")
    account_number: Mapped[str] = mapped_column(db.String(60), default="")
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"), index=True)
    currency_code: Mapped[str] = mapped_column(db.String(3))
    opening_balance: Mapped[Decimal] = mapped_column(Amount(), default=0)
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    branch = db.relationship("Branch")
    account = db.relationship("Account")

    @property
    def kind(self):
        return "bank"


class TreasuryTxn(TimestampMixin, db.Model):
    """A cash/bank movement document (spec §6). Every txn links to a posted
    journal entry — or waits for owner approval before posting (§6 limits)."""

    __tablename__ = "treasury_txn"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today, index=True)
    kind: Mapped[str] = mapped_column(db.String(20))
    # deposit / withdraw / internal_transfer / fx_transfer / bank_fee

    # GL accounts for the money flow: dst is debited, src is credited (primary).
    src_account_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))
    dst_account_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))

    amount_original: Mapped[Decimal] = mapped_column(Amount(), default=0)
    currency_code: Mapped[str] = mapped_column(db.String(3))
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)

    # cross-currency transfers only
    dst_currency_code: Mapped[str | None] = mapped_column(db.String(3))
    dst_fx_rate: Mapped[Decimal | None] = mapped_column(Rate())
    received_amount: Mapped[Decimal | None] = mapped_column(Amount())
    bank_fee: Mapped[Decimal | None] = mapped_column(Amount())

    status: Mapped[str] = mapped_column(db.String(20), default="posted")
    # posted / pending_approval
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id")
    )
    approved_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    memo: Mapped[str | None] = mapped_column(db.String(255))

    journal_entry = db.relationship("JournalEntry")
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    @property
    def kind_label(self):
        return {
            "deposit": "إيداع", "withdraw": "سحب",
            "internal_transfer": "تحويل داخلي", "fx_transfer": "تحويل بعملتين",
            "bank_fee": "مصروف بنكي",
        }.get(self.kind, self.kind)


class ReconItem(db.Model):
    """Bank reconciliation mark (spec §6). Ties a journal line on a bank
    account to a 'cleared' state against the statement."""

    __tablename__ = "recon_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(db.ForeignKey("bank_account.id"))
    journal_line_id: Mapped[int] = mapped_column(db.ForeignKey("journal_line.id"))
    cleared: Mapped[bool] = mapped_column(default=True)
    statement_ref: Mapped[str | None] = mapped_column(db.String(80))
    reconciled_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    reconciled_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
