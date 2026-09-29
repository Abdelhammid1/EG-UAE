"""Data-migration / opening balances (spec §M13, MIG-01..04)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.core.mixins import TimestampMixin
from app.extensions import db


class ImportBatch(TimestampMixin, db.Model):
    """One uploaded file, validated then committed. All-or-nothing (MIG-03)."""

    __tablename__ = "import_batch"

    kind: Mapped[str] = mapped_column(db.String(20))  # products/stock/treasuries/...
    filename: Mapped[str | None] = mapped_column(db.String(255))
    status: Mapped[str] = mapped_column(db.String(12), default="validated")
    # validated / committed
    rows_json: Mapped[str | None] = mapped_column(db.Text)  # parsed rows
    row_count: Mapped[int] = mapped_column(default=0)
    error_count: Mapped[int] = mapped_column(default=0)
    committed_at: Mapped[datetime | None] = mapped_column(nullable=True)
