"""Inter-warehouse transfers with landed cost (spec §8).

A transfer permit moves stock between warehouses. On send, the goods' book value
leaves inventory for a goods-in-transit account (so warehouse reports don't look
short). Shipping and customs are capitalized into transit, then allocated across
the items by value / quantity / weight. On receipt, each item lands at its
original cost plus its share of freight and customs.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Qty, Rate
from app.extensions import db

ALLOCATION_METHODS = {"value": "بالقيمة", "quantity": "بالكمية", "weight": "بالوزن"}
STATUS_LABELS = {"draft": "مسودة", "sent": "في الطريق", "received": "مستلمة",
                 "closed": "مغلقة", "cancelled": "ملغاة"}


class TransferPermit(TimestampMixin, db.Model):
    __tablename__ = "transfer_permit"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today)
    from_warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    to_warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    status: Mapped[str] = mapped_column(db.String(10), default="draft")
    # destination currency -> book rate, approved on the permit (§8.6)
    fx_rate: Mapped[Decimal] = mapped_column(Rate(), default=1)
    allocation_method: Mapped[str] = mapped_column(db.String(10), default="value")
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    from_warehouse = db.relationship("Warehouse", foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse", foreign_keys=[to_warehouse_id])
    lines = db.relationship("TransferLine", back_populates="permit",
                            cascade="all, delete-orphan")
    expenses = db.relationship("TransferExpense", back_populates="permit",
                               cascade="all, delete-orphan")

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def goods_book(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum(
            (l.qty_sent * l.unit_cost_book for l in self.lines), Decimal("0")))

    @property
    def expenses_book(self):
        from app.core.money import quantize_amount
        return quantize_amount(sum((e.amount_book for e in self.expenses), Decimal("0")))


class TransferLine(db.Model):
    __tablename__ = "transfer_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    permit_id: Mapped[int] = mapped_column(db.ForeignKey("transfer_permit.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty_sent: Mapped[Decimal] = mapped_column(Qty(), default=0)
    qty_received: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_cost_book: Mapped[Decimal] = mapped_column(Amount(), default=0)  # source cost
    alloc_shipping_book: Mapped[Decimal] = mapped_column(Amount(), default=0)
    alloc_customs_book: Mapped[Decimal] = mapped_column(Amount(), default=0)

    permit = db.relationship("TransferPermit", back_populates="lines")
    product = db.relationship("Product")

    @property
    def final_unit_cost_book(self):
        from app.core.money import quantize_amount
        if not self.qty_sent:
            return quantize_amount(self.unit_cost_book)
        share = (self.alloc_shipping_book + self.alloc_customs_book) / self.qty_sent
        return quantize_amount(self.unit_cost_book + share)

    def final_unit_cost_native(self, fx_rate):
        from app.core.money import quantize_amount, to_decimal
        r = to_decimal(fx_rate) or Decimal("1")
        return quantize_amount(self.final_unit_cost_book / r)


class TransferExpense(db.Model):
    __tablename__ = "transfer_expense"

    id: Mapped[int] = mapped_column(primary_key=True)
    permit_id: Mapped[int] = mapped_column(db.ForeignKey("transfer_permit.id"))
    kind: Mapped[str] = mapped_column(db.String(12))  # shipping / customs
    amount_book: Mapped[Decimal] = mapped_column(Amount(), default=0)
    paid_from_account_id: Mapped[int] = mapped_column(db.ForeignKey("account.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id"))
    note: Mapped[str | None] = mapped_column(db.String(255))

    permit = db.relationship("TransferPermit", back_populates="expenses")

    @property
    def kind_label(self):
        return {"shipping": "شحن", "customs": "جمارك"}.get(self.kind, self.kind)
