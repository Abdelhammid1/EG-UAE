"""Inventory: products, warehouses, stock batches, and cash sales.

This is the Phase 2.5 vertical slice — deliberately minimal but real. It runs
in the book currency so the full chain (sale -> revenue + COGS -> cash into
treasury -> stock out) is consistent. Phase 3 extends valuation to per-country
currency and adds transfers, stocktakes, serials, etc.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.core.money import Amount, Qty
from app.extensions import db

VALUATION_METHODS = {"FIFO": "الوارد أولًا صادر أولًا", "WA": "المتوسط المرجح"}


class Product(TimestampMixin, db.Model):
    """Spec §7 product card (minimal slice)."""

    __tablename__ = "product"

    name_ar: Mapped[str] = mapped_column(db.String(160))
    barcode: Mapped[str | None] = mapped_column(db.String(60), index=True)
    category: Mapped[str | None] = mapped_column(db.String(80))
    unit: Mapped[str] = mapped_column(db.String(20), default="قطعة")
    default_price: Mapped[Decimal] = mapped_column(Amount(), default=0)  # retail (قطاعي)
    wholesale_price: Mapped[Decimal] = mapped_column(Amount(), default=0)  # جملة (SAL-02)
    min_price: Mapped[Decimal] = mapped_column(Amount(), default=0)
    valuation_method: Mapped[str] = mapped_column(db.String(8), default="FIFO")
    reorder_level: Mapped[Decimal] = mapped_column(Qty(), default=0)
    track_serial: Mapped[bool] = mapped_column(default=False)  # IMEI (§7.3)
    weight: Mapped[Decimal] = mapped_column(Qty(), default=0)  # optional (§4)
    image_path: Mapped[str | None] = mapped_column(db.String(255))  # product photo

    def price_for(self, customer=None):
        """Retail by default; the wholesale price for a wholesale customer when
        one is set (spec SAL-02)."""
        if (customer is not None and getattr(customer, "type", None) == "wholesale"
                and self.wholesale_price and self.wholesale_price > 0):
            return self.wholesale_price
        return self.default_price

    def __repr__(self):
        return f"<Product {self.name_ar}>"


class Warehouse(TimestampMixin, db.Model):
    """Spec §4 warehouse (minimal slice)."""

    __tablename__ = "warehouse"

    name_ar: Mapped[str] = mapped_column(db.String(120))
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"), index=True)

    branch = db.relationship("Branch")


class StockBatch(TimestampMixin, db.Model):
    """A received lot carrying its own valuation method (spec §7.2, §7.3)."""

    __tablename__ = "stock_batch"

    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"), index=True)
    qty_received: Mapped[Decimal] = mapped_column(Qty(), default=0)
    qty_remaining: Mapped[Decimal] = mapped_column(Qty(), default=0)
    # cost in the warehouse's own currency (§7.5) + book equivalent (§5.2)
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)  # native
    unit_cost_book: Mapped[Decimal] = mapped_column(Amount(), default=0)  # AED
    valuation_method: Mapped[str] = mapped_column(db.String(8), default="FIFO")
    source_doc: Mapped[str | None] = mapped_column(db.String(40))

    product = db.relationship("Product")
    warehouse = db.relationship("Warehouse")


class StockMovement(db.Model):
    """Every quantity change is a movement (audit trail for stock)."""

    __tablename__ = "stock_movement"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    batch_id: Mapped[int | None] = mapped_column(db.ForeignKey("stock_batch.id"))
    direction: Mapped[str] = mapped_column(db.String(4))  # in / out
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)
    doc_type: Mapped[str] = mapped_column(db.String(40))
    doc_id: Mapped[int | None] = mapped_column(nullable=True)


class Sale(TimestampMixin, db.Model):
    """A cash sale document (slice). Links to its journal entry."""

    __tablename__ = "sale"

    number: Mapped[str | None] = mapped_column(db.String(40), index=True)
    date: Mapped[date] = mapped_column(default=date.today, index=True)
    branch_id: Mapped[int] = mapped_column(db.ForeignKey("branch.id"))
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    treasury_id: Mapped[int | None] = mapped_column(db.ForeignKey("treasury.id"))
    currency_code: Mapped[str] = mapped_column(db.String(3), default="AED")
    subtotal: Mapped[Decimal] = mapped_column(Amount(), default=0)  # before tax
    tax_amount: Mapped[Decimal] = mapped_column(Amount(), default=0)
    total: Mapped[Decimal] = mapped_column(Amount(), default=0)  # incl. tax
    total_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)  # book (AED)
    below_min_used: Mapped[bool] = mapped_column(default=False)
    # sales-domain fields (§10)
    customer_id: Mapped[int | None] = mapped_column(db.ForeignKey("customer.id"))
    payment_type: Mapped[str] = mapped_column(db.String(12), default="cash")
    shift_id: Mapped[int | None] = mapped_column(db.ForeignKey("shift.id"))
    returned: Mapped[bool] = mapped_column(default=False)
    funder_id: Mapped[int | None] = mapped_column(db.ForeignKey("funder.id"))
    funder_settled: Mapped[bool] = mapped_column(default=False)
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id")
    )

    warehouse = db.relationship("Warehouse")
    treasury = db.relationship("Treasury")
    customer = db.relationship("Customer")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("SaleLine", back_populates="sale",
                            cascade="all, delete-orphan")

    @property
    def profit(self):
        from app.core.money import quantize_amount
        return quantize_amount(self.total - self.total_cost)


class SaleLine(db.Model):
    __tablename__ = "sale_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(db.ForeignKey("sale.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_price: Mapped[Decimal] = mapped_column(Amount(), default=0)
    line_total: Mapped[Decimal] = mapped_column(Amount(), default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Amount(), default=0)
    cogs: Mapped[Decimal] = mapped_column(Amount(), default=0)

    sale = db.relationship("Sale", back_populates="lines")
    product = db.relationship("Product")


class ProductSerial(db.Model):
    """Serial/IMEI tracking (spec §7.3, §16.14, §16.15). A serial is received
    once and sold once — never sold twice."""

    __tablename__ = "product_serial"
    __table_args__ = (db.UniqueConstraint("product_id", "serial"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"), index=True)
    serial: Mapped[str] = mapped_column(db.String(80), index=True)
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    batch_id: Mapped[int | None] = mapped_column(db.ForeignKey("stock_batch.id"))
    status: Mapped[str] = mapped_column(db.String(12), default="in_stock")  # in_stock/sold
    received_doc: Mapped[str | None] = mapped_column(db.String(40))
    sold_sale_id: Mapped[int | None] = mapped_column(db.ForeignKey("sale.id"))

    product = db.relationship("Product")


class Stocktake(TimestampMixin, db.Model):
    """A physical count session (spec §7 الجرد الدوري)."""

    __tablename__ = "stocktake"

    number: Mapped[str | None] = mapped_column(db.String(40))
    warehouse_id: Mapped[int] = mapped_column(db.ForeignKey("warehouse.id"))
    date: Mapped[date] = mapped_column(default=date.today)
    status: Mapped[str] = mapped_column(db.String(12), default="open")  # open/approved
    approved_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    journal_entry_id: Mapped[int | None] = mapped_column(
        db.ForeignKey("journal_entry.id")
    )

    warehouse = db.relationship("Warehouse")
    lines = db.relationship("StocktakeLine", back_populates="stocktake",
                            cascade="all, delete-orphan")


class StocktakeLine(db.Model):
    __tablename__ = "stocktake_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    stocktake_id: Mapped[int] = mapped_column(db.ForeignKey("stocktake.id"))
    product_id: Mapped[int] = mapped_column(db.ForeignKey("product.id"))
    system_qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    counted_qty: Mapped[Decimal] = mapped_column(Qty(), default=0)
    unit_cost_book: Mapped[Decimal] = mapped_column(Amount(), default=0)

    stocktake = db.relationship("Stocktake", back_populates="lines")
    product = db.relationship("Product")

    @property
    def variance(self):
        from app.core.money import quantize_amount
        return quantize_amount(self.counted_qty - self.system_qty)
