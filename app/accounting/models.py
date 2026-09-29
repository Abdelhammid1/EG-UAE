"""Accounting kernel models (spec §13, §4).

Double-entry, immutable once posted. Every line carries the money triple
(original amount, fx rate, book equivalent). Chart of accounts is hierarchical
and only leaf accounts are postable.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import ImmutableAfterPost, TimestampMixin
from app.core.money import Amount, Rate
from app.extensions import db

# Account types (spec §4). Debit-normal: asset, expense. Credit-normal: the rest.
ACCOUNT_TYPES = {
    "asset": "أصول",
    "liability": "خصوم",
    "equity": "حقوق ملكية",
    "revenue": "إيرادات",
    "expense": "مصروفات",
}
DEBIT_NORMAL = {"asset", "expense"}


class Account(TimestampMixin, db.Model):
    """Chart of accounts — hierarchical (spec §13.1)."""

    __tablename__ = "account"

    code: Mapped[str] = mapped_column(db.String(30), unique=True, index=True)
    name_ar: Mapped[str] = mapped_column(db.String(160))
    name_en: Mapped[str] = mapped_column(db.String(160), default="")
    type: Mapped[str] = mapped_column(db.String(20))
    parent_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))
    # Only leaf accounts accept postings; parents are for grouping/rollup.
    is_postable: Mapped[bool] = mapped_column(default=True)

    parent = db.relationship("Account", remote_side="Account.id", backref="children")

    @property
    def is_debit_normal(self) -> bool:
        return self.type in DEBIT_NORMAL

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPES.get(self.type, self.type)

    def __repr__(self):
        return f"<Account {self.code} {self.name_ar}>"


class AccountingPeriod(TimestampMixin, db.Model):
    """Open/closed periods. Closing/reopening needs owner approval (spec §13.6)."""

    __tablename__ = "accounting_period"

    name: Mapped[str] = mapped_column(db.String(60))  # e.g. "2026-Q1" or "يناير 2026"
    start_date: Mapped[date] = mapped_column()
    end_date: Mapped[date] = mapped_column()
    status: Mapped[str] = mapped_column(db.String(10), default="open")  # open/closed
    closed_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    approved_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    closed_by = db.relationship("User", foreign_keys=[closed_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def contains(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


class JournalEntry(ImmutableAfterPost, TimestampMixin, db.Model):
    """A balanced double-entry document. Immutable once posted (spec §13.5)."""

    __tablename__ = "journal_entry"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today, index=True)
    branch_id: Mapped[int | None] = mapped_column(db.ForeignKey("branch.id"), index=True)
    period_id: Mapped[int | None] = mapped_column(db.ForeignKey("accounting_period.id"))

    # Where this entry came from: a document type + id, or 'manual'.
    source_doc_type: Mapped[str] = mapped_column(db.String(40), default="manual")
    source_doc_id: Mapped[int | None] = mapped_column(nullable=True)

    status: Mapped[str] = mapped_column(db.String(10), default="draft")  # draft/posted
    memo: Mapped[str | None] = mapped_column(db.Text)

    posted_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    posted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Reversal link (spec §13.5): a correcting entry points at the original.
    reverses_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id")
    )

    branch = db.relationship("Branch")
    period = db.relationship("AccountingPeriod")
    posted_by = db.relationship("User", foreign_keys=[posted_by_id])
    reverses = db.relationship("JournalEntry", remote_side="JournalEntry.id")
    lines = db.relationship(
        "JournalLine", back_populates="entry", cascade="all, delete-orphan"
    )

    @property
    def total_debit_book(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum((l.debit_book or 0) for l in self.lines))

    @property
    def total_credit_book(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum((l.credit_book or 0) for l in self.lines))

    @property
    def is_balanced(self) -> bool:
        return self.total_debit_book == self.total_credit_book

    @property
    def is_posted(self) -> bool:
        return self.status == "posted"


class JournalLine(db.Model):
    """One side of one account's movement, with the money triple (spec §5.2)."""

    __tablename__ = "journal_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(db.ForeignKey("journal_entry.id"))
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    currency_code: Mapped[str] = mapped_column(db.String(3))
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)

    debit_original: Mapped[Decimal] = mapped_column(Amount(), default=0)
    credit_original: Mapped[Decimal] = mapped_column(Amount(), default=0)
    debit_book: Mapped[Decimal] = mapped_column(Amount(), default=0)
    credit_book: Mapped[Decimal] = mapped_column(Amount(), default=0)

    memo: Mapped[str | None] = mapped_column(db.String(255))

    entry = db.relationship("JournalEntry", back_populates="lines")
    account = db.relationship("Account")


class AccountMapping(db.Model):
    """Binds a business operation (spec §13 table) to an account — configuration,
    never hardcoded. E.g. 'sales.revenue' -> account #41."""

    __tablename__ = "account_mapping"
    __table_args__ = (UniqueConstraint("operation_code", "branch_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_code: Mapped[str] = mapped_column(db.String(60), index=True)
    branch_id: Mapped[int | None] = mapped_column(db.ForeignKey("branch.id"))
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    account = db.relationship("Account")


class ExchangeRate(db.Model):
    """Rate log (spec §5.3): every rate used is stored with its source and who
    entered it."""

    __tablename__ = "exchange_rate"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_currency: Mapped[str] = mapped_column(db.String(3), index=True)
    to_currency: Mapped[str] = mapped_column(db.String(3), index=True)
    rate: Mapped[Decimal] = mapped_column(Rate())
    at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    source: Mapped[str] = mapped_column(db.String(20), default="manual")  # auto/manual
    entered_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))

    entered_by = db.relationship("User")
