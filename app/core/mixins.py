"""Reusable model mixins that encode spec-wide rules.

Every entity carries id / created / updated / who / status (spec §4).
Every money-bearing row carries the three-value FX triple (spec §5.2).
"""
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from decimal import Decimal

from app.core.money import Amount, Rate
from app.extensions import db


class TimestampMixin:
    """id, created_at, updated_at, created_by, updated_by, is_active.

    Suspended entities are never deleted (spec §4). is_active=False = موقوف.
    """

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )
    is_active: Mapped[bool] = mapped_column(default=True)

    @declared_attr
    def created_by_id(cls) -> Mapped[int | None]:
        return mapped_column(db.ForeignKey("user.id"), nullable=True)

    @declared_attr
    def updated_by_id(cls) -> Mapped[int | None]:
        return mapped_column(db.ForeignKey("user.id"), nullable=True)


class MoneyTripleMixin:
    """The spec §5.2 triple: original amount, fx rate, book-currency equivalent.

    Stored, never recomputed on read, so historical documents never shift when
    an exchange rate later changes.
    """

    amount_original: Mapped[Decimal] = mapped_column(Amount(), default=0)
    currency_code: Mapped[str] = mapped_column(db.String(3))
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)
    amount_book: Mapped[Decimal] = mapped_column(Amount(), default=0)


class ImmutableAfterPost:
    """Marker mixin. Models tagged with this + a `status` of 'posted' cannot be
    updated or deleted; the guard in app.core.audit enforces it at the ORM level
    (and Postgres triggers enforce it again in production)."""

    __immutable_when_posted__ = True
