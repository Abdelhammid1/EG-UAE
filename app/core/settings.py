"""Settings service — the single gateway for reading and changing settings.

Nothing else in the codebase should read setting rows directly. This enforces
the spec's promises in one place:
  * get(key, at=...) returns the value that applied at a moment in time (§1.4).
  * set(...) refuses locked settings, records an audit entry, and appends a new
    time-effective value instead of editing in place (§2).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.admin.models import SettingDefinition, SettingValue
from app.core import audit
from app.core.money import to_decimal
from app.extensions import db


class SettingLockedError(Exception):
    pass


class SettingNotFoundError(Exception):
    pass


def _cast(value_type: str, raw):
    if raw is None:
        return None
    if value_type == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on", "نعم")
    if value_type == "int":
        return int(raw)
    if value_type == "decimal":
        return to_decimal(raw)
    return str(raw)


def get(key: str, scope_id: int | None = None, at: datetime | None = None):
    """Return the effective value of `key` at time `at` (default: now)."""
    definition = db.session.scalar(
        db.select(SettingDefinition).filter_by(key=key)
    )
    if definition is None:
        raise SettingNotFoundError(f"إعداد غير معرّف: {key}")

    at = at or datetime.utcnow()
    q = (
        db.select(SettingValue)
        .filter(SettingValue.definition_id == definition.id)
        .filter(SettingValue.effective_from <= at)
        .order_by(SettingValue.effective_from.desc())
    )
    if scope_id is not None:
        q = q.filter(SettingValue.scope_id == scope_id)
    else:
        q = q.filter(SettingValue.scope_id.is_(None))

    row = db.session.scalars(q).first()
    raw = row.value_text if row else definition.default_value
    return _cast(definition.value_type, raw)


def set(
    key: str,
    value,
    *,
    scope_id: int | None = None,
    effective_from: datetime | None = None,
    user_id: int | None = None,
    approved_by_id: int | None = None,
):
    """Change a setting: append a new time-effective value + audit it.

    Raises SettingLockedError for locked settings (spec §2 locked table).
    Callers are expected to have shown the confirmation dialog first.
    """
    definition = db.session.scalar(
        db.select(SettingDefinition).filter_by(key=key)
    )
    if definition is None:
        raise SettingNotFoundError(f"إعداد غير معرّف: {key}")
    if definition.is_locked:
        raise SettingLockedError(
            "هذا الإعداد مقفول بعد أول حركة ولا يمكن تعديله. لتغيير العملة يُنشأ كيان جديد."
        )

    old = get(key, scope_id=scope_id)
    new_text = "true" if value is True else ("false" if value is False else str(value))

    row = SettingValue(
        definition_id=definition.id,
        scope_type=definition.scope,
        scope_id=scope_id,
        value_text=new_text,
        effective_from=effective_from or datetime.utcnow(),
        created_by_id=user_id,
        approved_by_id=approved_by_id,
    )
    db.session.add(row)

    audit.record(
        action="setting.change",
        entity="setting",
        entity_id=key,
        old=str(old),
        new=new_text,
    )
    return row


def lock(key: str):
    """Permanently lock a setting after its first movement (spec §2)."""
    definition = db.session.scalar(
        db.select(SettingDefinition).filter_by(key=key)
    )
    if definition and definition.locks_after_first_use:
        definition.is_locked = True
        audit.record(action="setting.lock", entity="setting", entity_id=key)


def history(key: str):
    """All past values of a setting, newest first (for the inline history)."""
    definition = db.session.scalar(
        db.select(SettingDefinition).filter_by(key=key)
    )
    if definition is None:
        return []
    return definition.values
