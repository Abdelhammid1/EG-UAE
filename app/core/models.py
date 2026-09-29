"""Core reference entities: currencies, branches, document numbering."""
from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.extensions import db


class Currency(TimestampMixin, db.Model):
    """Spec §4: code, name, decimal places. Book currency is locked (§1.2)."""

    __tablename__ = "currency"

    code: Mapped[str] = mapped_column(db.String(3), unique=True, index=True)
    name_ar: Mapped[str] = mapped_column(db.String(80))
    name_en: Mapped[str] = mapped_column(db.String(80))
    decimal_places: Mapped[int] = mapped_column(default=2)
    symbol: Mapped[str | None] = mapped_column(db.String(8))

    def __repr__(self):
        return f"<Currency {self.code}>"


class Country(TimestampMixin, db.Model):
    """Spec §4: name, code, local currency. Open-ended (§1.6)."""

    __tablename__ = "country"

    name_ar: Mapped[str] = mapped_column(db.String(80))
    name_en: Mapped[str] = mapped_column(db.String(80))
    iso_code: Mapped[str] = mapped_column(db.String(3), unique=True)
    currency_code: Mapped[str] = mapped_column(db.ForeignKey("currency.code"))

    currency = db.relationship("Currency")


class Branch(TimestampMixin, db.Model):
    """Spec §4/§8: a branch belongs to a country and is configured for
    warehouses from settings. Open-ended count (§1.6)."""

    __tablename__ = "branch"

    name_ar: Mapped[str] = mapped_column(db.String(120))
    name_en: Mapped[str] = mapped_column(db.String(120))
    country_id: Mapped[int] = mapped_column(db.ForeignKey("country.id"))

    country = db.relationship("Country")

    def __repr__(self):
        return f"<Branch {self.name_ar}>"


class DocumentSequence(db.Model):
    """Per-branch, per-document-type running number (spec §4.1).

    next_number is advanced under a row lock inside the same transaction as the
    insert, which is the fix for the concurrent-numbering edge case (§16.22).
    """

    __tablename__ = "document_sequence"
    __table_args__ = (UniqueConstraint("branch_id", "doc_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    doc_type: Mapped[str] = mapped_column(db.String(40))
    prefix: Mapped[str] = mapped_column(db.String(20), default="")
    next_number: Mapped[int] = mapped_column(default=1)
