"""The money layer. This is the foundation the whole system rests on.

Rules enforced here (spec §1.1, §5.2, §15.1):
  * Amounts are Decimal, never float.
  * One rounding rule for the entire system: ROUND_HALF_UP.
  * Decimal places are per-currency, read from the currency table.
  * Every stored money value keeps three parts: original amount, fx rate,
    and the book-currency (AED) equivalent computed at the time of the event.

The DecimalText column type stores Decimal safely on BOTH SQLite (as TEXT, so
no float rounding ever happens) and PostgreSQL (as NUMERIC). It always returns
a Decimal, so application code never sees a float.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator

# The single rounding rule for the whole system.
ROUNDING = ROUND_HALF_UP

# Storage scales. Amounts keep 4 places internally; presentation rounds to the
# currency's own scale. FX rates keep 8 places.
AMOUNT_SCALE = Decimal("0.0001")
RATE_SCALE = Decimal("0.00000001")


def to_decimal(value) -> Decimal:
    """Coerce any input to Decimal without ever going through float."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"قيمة رقمية غير صالحة: {value!r}")


def quantize_amount(value) -> Decimal:
    return to_decimal(value).quantize(AMOUNT_SCALE, rounding=ROUNDING)


def quantize_rate(value) -> Decimal:
    return to_decimal(value).quantize(RATE_SCALE, rounding=ROUNDING)


def round_to_currency(value, decimal_places: int) -> Decimal:
    """Round for presentation/settlement to a currency's own scale."""
    q = Decimal(1).scaleb(-decimal_places)  # e.g. 2 -> Decimal('0.01')
    return to_decimal(value).quantize(q, rounding=ROUNDING)


class DecimalText(TypeDecorator):
    """Decimal that is exact on SQLite and native NUMERIC on PostgreSQL.

    On SQLite we persist as TEXT to avoid the REAL/float affinity that would
    corrupt financial precision. On PostgreSQL we use NUMERIC(precision, scale).
    """

    impl = String  # default; overridden per-dialect in load_dialect_impl
    cache_ok = True

    def __init__(self, precision=19, scale=4, **kw):
        self.precision = precision
        self.scale = scale
        super().__init__(**kw)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(self.precision, self.scale))
        return dialect.type_descriptor(String(40))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = to_decimal(value).quantize(
            Decimal(1).scaleb(-self.scale), rounding=ROUNDING
        )
        if dialect.name == "postgresql":
            return d
        return format(d, "f")  # exact decimal string, never scientific notation

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return to_decimal(value)


def Amount():
    """Column type for monetary amounts: NUMERIC(19,4) / exact text."""
    return DecimalText(precision=19, scale=4)


def Rate():
    """Column type for FX rates: NUMERIC(19,8) / exact text."""
    return DecimalText(precision=19, scale=8)


def Qty():
    """Column type for inventory quantities: NUMERIC(19,4) / exact text."""
    return DecimalText(precision=19, scale=4)


class Money:
    """A value object bundling an amount with its currency.

    Refuses to add two different currencies — a mismatch is a bug, not a
    silent conversion.
    """

    __slots__ = ("amount", "currency")

    def __init__(self, amount, currency: str):
        self.amount = quantize_amount(amount)
        self.currency = currency.upper()

    def _check(self, other: "Money"):
        if self.currency != other.currency:
            raise ValueError(
                f"لا يمكن الجمع بين عملتين مختلفتين: {self.currency} و {other.currency}"
            )

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def to_book(self, fx_rate) -> "Money":
        """Convert to the book currency (AED) at a given rate."""
        from flask import current_app

        book = current_app.config["BOOK_CURRENCY"]
        return Money(self.amount * quantize_rate(fx_rate), book)

    def __repr__(self):
        return f"Money({self.amount} {self.currency})"

    def __eq__(self, other):
        return (
            isinstance(other, Money)
            and self.amount == other.amount
            and self.currency == other.currency
        )
