"""Customers, cashier shifts, and sales returns (spec §10, §11).

Each customer owns an AR account (child of ذمم العملاء), so balance and
statement come from the ledger. A shift ties POS sales to a drawer and is
reconciled at close (over/short). Returns reverse a sale at its original cost.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Qty
from app.extensions import db

CUSTOMER_TYPES = {"retail": "قطاعي", "wholesale": "جملة"}


class Customer(TimestampMixin, db.Model):
    __tablename__ = "customer"

    name_ar: Mapped[str] = mapped_column(db.String(160))
    phone: Mapped[str | None] = mapped_column(db.String(40))
    type: Mapped[str] = mapped_column(db.String(10), default="retail")
    credit_limit: Mapped[Decimal] = mapped_column(Amount(), default=0)
    opening_balance: Mapped[Decimal] = mapped_column(Amount(), default=0)
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    account = db.relationship("Account")

    @property
    def type_label(self):
        return CUSTOMER_TYPES.get(self.type, self.type)

    @property
    def kind(self):
        return "customer"


class Shift(TimestampMixin, db.Model):
    """A cashier session on one drawer (spec §11)."""

    __tablename__ = "shift"

    number: Mapped[str | None] = mapped_column(db.String(40))
    cashier_id: Mapped[int] = mapped_column(db.ForeignKey("user.id"))
    drawer_id: Mapped[int] = mapped_column(db.ForeignKey("treasury.id"))
    opening_cash: Mapped[Decimal] = mapped_column(Amount(), default=0)
    counted_cash: Mapped[Decimal] = mapped_column(Amount(), default=0)
    expected_cash: Mapped[Decimal] = mapped_column(Amount(), default=0)
    over_short: Mapped[Decimal] = mapped_column(Amount(), default=0)  # +over / -short
    status: Mapped[str] = mapped_column(db.String(8), default="open")
    opened_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    cashier = db.relationship("User", foreign_keys=[cashier_id])
    drawer = db.relationship("Treasury")


class SalesReturn(TimestampMixin, db.Model):
    __tablename__ = "sales_return"

    number: Mapped[str | None] = mapped_column(db.String(40))
    date: Mapped[date] = mapped_column(default=date.today)
    original_sale_id: Mapped[int] = mapped_column(db.ForeignKey("sale.id"))
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    total: Mapped[Decimal] = mapped_column(Amount(), default=0)
    total_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    original_sale = db.relationship("Sale")
    lines = db.relationship("SalesReturnLine", back_populates="ret",
                            cascade="all, delete-orphan")


class SalesReturnLine(db.Model):
    __tablename__ = "sales_return_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(db.ForeignKey("sales_return.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_price: Mapped[Decimal] = mapped_column(Amount(), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)  # book

    ret = db.relationship("SalesReturn", back_populates="lines")
    product = db.relationship("Product")


class Collection(TimestampMixin, db.Model):
    """A payment received from a customer against their AR (spec §10.4)."""

    __tablename__ = "collection"

    number: Mapped[str | None] = mapped_column(db.String(40))
    date: Mapped[date] = mapped_column(default=date.today)
    customer_id: Mapped[int] = mapped_column(db.ForeignKey("customer.id"))
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    to_account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    customer = db.relationship("Customer")


class Funder(TimestampMixin, db.Model):
    """A BNPL provider (تابي/تمارا). Its settings have NO defaults (spec §10)."""

    __tablename__ = "funder"

    name_ar: Mapped[str] = mapped_column(db.String(120))
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    commission_pct: Mapped[Decimal] = mapped_column(Amount(), default=0)
    fixed_fee: Mapped[Decimal] = mapped_column(Amount(), default=0)
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))  # funder AR
    settlement_bank_account_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("account.id"))

    account = db.relationship("Account", foreign_keys=[account_id])

    @property
    def kind(self):
        return "funder"


class FunderSettlement(TimestampMixin, db.Model):
    __tablename__ = "funder_settlement"

    number: Mapped[str | None] = mapped_column(db.String(40))
    date: Mapped[date] = mapped_column(default=date.today)
    funder_id: Mapped[int] = mapped_column(db.ForeignKey("funder.id"))
    invoices_total: Mapped[Decimal] = mapped_column(Amount(), default=0)
    received_amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    commission: Mapped[Decimal] = mapped_column(Amount(), default=0)
    bank_account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    funder = db.relationship("Funder")


class InstallmentPlan(TimestampMixin, db.Model):
    """Internal installment schedule on a credit sale (spec §10.5)."""

    __tablename__ = "installment_plan"

    sale_id: Mapped[int] = mapped_column(db.ForeignKey("sale.id"))
    customer_id: Mapped[int] = mapped_column(db.ForeignKey("customer.id"))
    count: Mapped[int] = mapped_column(default=1)
    down_payment: Mapped[Decimal] = mapped_column(Amount(), default=0)

    customer = db.relationship("Customer")
    dues = db.relationship("InstallmentDue", back_populates="plan",
                           cascade="all, delete-orphan", order_by="InstallmentDue.seq")


class InstallmentDue(db.Model):
    __tablename__ = "installment_due"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(db.ForeignKey("installment_plan.id"))
    seq: Mapped[int] = mapped_column(default=1)
    due_date: Mapped[date] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    paid_amount: Mapped[Decimal] = mapped_column(Amount(), default=0)

    plan = db.relationship("InstallmentPlan", back_populates="dues")

    @property
    def outstanding(self):
        from app.core.money import quantize_amount
        return quantize_amount(self.amount - self.paid_amount)

    @property
    def is_paid(self):
        return self.outstanding <= 0


class Quote(TimestampMixin, db.Model):
    """A price quote (spec SAL-08). No stock hold, no ledger entry."""

    __tablename__ = "quote"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today)
    customer_id: Mapped[int | None] = mapped_column(db.ForeignKey("customer.id"))
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    valid_until: Mapped[date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(db.String(12), default="draft")
    # draft / sent / accepted / expired / cancelled
    sale_id: Mapped[int | None] = mapped_column(db.ForeignKey("sale.id"))

    customer = db.relationship("Customer")
    warehouse = db.relationship("Warehouse")
    lines = db.relationship("QuoteLine", back_populates="quote",
                            cascade="all, delete-orphan")

    @property
    def total(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum((l.qty * l.unit_price for l in self.lines),
                                   Decimal("0")))

    @property
    def status_label(self):
        return {"draft": "مسودة", "sent": "مُرسل", "accepted": "مقبول",
                "expired": "منتهٍ", "cancelled": "ملغى"}.get(self.status, self.status)


class QuoteLine(db.Model):
    __tablename__ = "quote_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(db.ForeignKey("quote.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_price: Mapped[Decimal] = mapped_column(Amount(), default=0)

    quote = db.relationship("Quote", back_populates="lines")
    product = db.relationship("Product")


class SalePayment(db.Model):
    """One tender in a (possibly split) sale payment (spec POS-03)."""

    __tablename__ = "sale_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(db.ForeignKey("sale.id"))
    method: Mapped[str] = mapped_column(db.String(12))  # cash / card / credit
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    account_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))


class HeldSale(TimestampMixin, db.Model):
    """A parked POS cart (spec POS-02.7 تعليق/استرجاع الفاتورة).

    The cart is stored as JSON so a cashier can hold a sale and recall it later
    (even on another terminal). Held sales never touch the ledger — they are just
    a saved draft, cleared once recalled or discarded.
    """

    __tablename__ = "held_sale"

    label: Mapped[str] = mapped_column(db.String(80))  # customer name / note
    branch_id: Mapped[int | None] = mapped_column(db.ForeignKey("branch.id"))
    cashier_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    payload: Mapped[str] = mapped_column(db.Text)  # JSON of the cart + options

    cashier = db.relationship("User", foreign_keys=[cashier_id])
