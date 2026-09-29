"""Suppliers and purchasing (spec §9).

Each supplier owns a dedicated AP account (child of ذمم الموردين), so its
balance and statement come straight from the ledger. Purchase invoices receive
stock and post the entry; the credit side depends on how they're paid.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Qty, Rate
from app.extensions import db


class Supplier(TimestampMixin, db.Model):
    __tablename__ = "supplier"

    name_ar: Mapped[str] = mapped_column(db.String(160))
    phone: Mapped[str | None] = mapped_column(db.String(40))
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    payment_terms_days: Mapped[int] = mapped_column(default=0)
    opening_balance: Mapped[Decimal] = mapped_column(Amount(), default=0)
    account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))

    account = db.relationship("Account")

    @property
    def kind(self):
        return "supplier"


class PurchaseInvoice(TimestampMixin, db.Model):
    __tablename__ = "purchase_invoice"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today, index=True)
    due_date: Mapped[date | None] = mapped_column(nullable=True)
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    supplier_id: Mapped[int | None] = mapped_column(db.ForeignKey("supplier.id"))
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    payment_type: Mapped[str] = mapped_column(db.String(10))  # cash/credit/custody
    pay_from_account_id: Mapped[int | None] = mapped_column(db.ForeignKey("account.id"))
    custody_id: Mapped[int | None] = mapped_column(db.ForeignKey("custody.id"))

    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)
    total: Mapped[Decimal] = mapped_column(Amount(), default=0)  # native
    total_book: Mapped[Decimal] = mapped_column(Amount(), default=0)
    status: Mapped[str] = mapped_column(db.String(10), default="posted")
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    supplier = db.relationship("Supplier")
    warehouse = db.relationship("Warehouse")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("PurchaseInvoiceLine", back_populates="invoice",
                            cascade="all, delete-orphan")


class PurchaseInvoiceLine(db.Model):
    __tablename__ = "purchase_invoice_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(db.ForeignKey("purchase_invoice.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)
    line_total: Mapped[Decimal] = mapped_column(Amount(), default=0)

    invoice = db.relationship("PurchaseInvoice", back_populates="lines")
    product = db.relationship("Product")


class SupplierPayment(TimestampMixin, db.Model):
    __tablename__ = "supplier_payment"

    number: Mapped[str | None] = mapped_column(db.String(40))
    date: Mapped[date] = mapped_column(default=date.today)
    supplier_id: Mapped[int] = mapped_column(db.ForeignKey("supplier.id"))
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)  # supplier currency
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)
    pay_from_account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))

    supplier = db.relationship("Supplier")
    allocations = db.relationship("PaymentAllocation", back_populates="payment",
                                  cascade="all, delete-orphan")


class PaymentAllocation(db.Model):
    __tablename__ = "payment_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(db.ForeignKey("supplier_payment.id"))
    invoice_id: Mapped[int] = mapped_column(db.ForeignKey("purchase_invoice.id"))
    amount: Mapped[Decimal] = mapped_column(Amount(), default=0)

    payment = db.relationship("SupplierPayment", back_populates="allocations")
    invoice = db.relationship("PurchaseInvoice")


class PurchaseOrder(TimestampMixin, db.Model):
    """Optional purchase order (spec PUR-01). Not binding on the ledger."""

    __tablename__ = "purchase_order"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today)
    supplier_id: Mapped[int | None] = mapped_column(db.ForeignKey("supplier.id"))
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    expected_date: Mapped[date | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(db.String(14), default="draft")
    # draft / approved / received / cancelled
    invoice_id: Mapped[int | None] = mapped_column(db.ForeignKey("purchase_invoice.id"))

    supplier = db.relationship("Supplier")
    warehouse = db.relationship("Warehouse")
    lines = db.relationship("PurchaseOrderLine", back_populates="order",
                            cascade="all, delete-orphan")

    @property
    def total(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum((l.qty * l.unit_cost for l in self.lines),
                                   Decimal("0")))

    @property
    def status_label(self):
        return {"draft": "مسودة", "approved": "معتمد", "received": "مستلم",
                "cancelled": "ملغى"}.get(self.status, self.status)


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(db.ForeignKey("purchase_order.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)

    order = db.relationship("PurchaseOrder", back_populates="lines")
    product = db.relationship("Product")
