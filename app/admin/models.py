"""Settings registry (spec §1.3, §2).

Every configurable value lives here — never as a literal in code. Reads are
time-aware: a document created last month sees the value that applied then.
Changes only affect new operations; existing documents keep their snapshot.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SettingDefinition(db.Model):
    """The catalogue of what can be configured, and how it behaves."""

    __tablename__ = "setting_definition"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(db.String(80), unique=True, index=True)
    section: Mapped[str] = mapped_column(db.String(60))  # §2 sections
    value_type: Mapped[str] = mapped_column(db.String(20))  # str/int/decimal/bool/json
    scope: Mapped[str] = mapped_column(db.String(20), default="global")  # global/branch
    label_ar: Mapped[str] = mapped_column(db.String(200))
    label_en: Mapped[str] = mapped_column(db.String(200))
    help_ar: Mapped[str | None] = mapped_column(db.Text)
    default_value: Mapped[str | None] = mapped_column(db.Text)

    # Locked after the first movement (book/treasury/account currency) — §2.
    locks_after_first_use: Mapped[bool] = mapped_column(default=False)
    is_locked: Mapped[bool] = mapped_column(default=False)

    # Sensitive settings that need the owner's approval before taking effect.
    requires_owner_approval: Mapped[bool] = mapped_column(default=False)

    values = db.relationship(
        "SettingValue",
        back_populates="definition",
        order_by="SettingValue.effective_from.desc()",
    )


class SettingValue(db.Model):
    """A value that took effect at a point in time, for a given scope.

    Values are never edited in place; a change appends a new row with a new
    effective_from. This is what makes reads time-aware and gives the inline
    change-history the spec requires (§2 rule 5)."""

    __tablename__ = "setting_value"

    id: Mapped[int] = mapped_column(primary_key=True)
    definition_id: Mapped[int] = mapped_column(db.ForeignKey("setting_definition.id"))
    scope_type: Mapped[str] = mapped_column(db.String(20), default="global")
    scope_id: Mapped[int | None] = mapped_column(nullable=True)  # e.g. branch id
    value_text: Mapped[str | None] = mapped_column(db.Text)
    effective_from: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)

    created_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    approved_by_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    definition = db.relationship("SettingDefinition", back_populates="values")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
