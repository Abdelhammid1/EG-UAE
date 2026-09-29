"""Parties / الأطراف (spec §M4, PTY-01..09).

A party is anyone you hold money with — a person carrying cash, a rep, etc.
Layered on top of the existing Customer/Supplier/Custody/Funder entities. Each
party gets a dedicated GL sub-account PER CURRENCY (auto-created on first use)
under 'أرصدة لدى الأشخاص', so a single party can hold AED + EGP + USD + SAR
balances at once, each its own locked-currency account.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Rate
from app.extensions import db


class Party(TimestampMixin, db.Model):
    __tablename__ = "party"

    name_ar: Mapped[str] = mapped_column(db.String(160))
    phone: Mapped[str | None] = mapped_column(db.String(40))
    address: Mapped[str | None] = mapped_column(db.String(255))
    types: Mapped[str] = mapped_column(db.String(120), default="person")  # csv
    branch_id: Mapped[int | None] = mapped_column(db.ForeignKey("branch.id"))
    notes: Mapped[str | None] = mapped_column(db.Text)

    accounts = db.relationship("PartyAccount", back_populates="party",
                               cascade="all, delete-orphan")

    @property
    def type_list(self):
        return [t.strip() for t in (self.types or "").split(",") if t.strip()]


class PartyAccount(db.Model):
    """One GL sub-account for a party in one currency (PTY-02)."""

    __tablename__ = "party_account"
    __table_args__ = (db.UniqueConstraint("party_id", "currency_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(db.ForeignKey("party.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3))
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    party = db.relationship("Party", back_populates="accounts")
    account = db.relationship("Account")


class PartyTxn(TimestampMixin, db.Model):
    """Classifies each party movement so the PTY-09 summary can split
    took / returned / spent."""

    __tablename__ = "party_txn"

    party_id: Mapped[int] = mapped_column(db.ForeignKey("party.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3))
    date: Mapped[date] = mapped_column(default=date.today)
    kind: Mapped[str] = mapped_column(db.String(14))
    # pay / refund / spend / transfer_in / transfer_out
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)  # party currency
    memo: Mapped[str | None] = mapped_column(db.String(255))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    party = db.relationship("Party")

    @property
    def kind_label(self):
        return {"pay": "صرف له", "refund": "استرداد", "spend": "صرف منه",
                "transfer_in": "تحويل وارد", "transfer_out": "تحويل صادر"}.get(
            self.kind, self.kind)
