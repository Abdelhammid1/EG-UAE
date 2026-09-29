"""Audit log and immutability enforcement (spec §15.1, §13.5).

The audit log is append-only: models flagged immutable cannot be updated or
deleted once posted, and audit rows can never be updated or deleted at all.
Enforced here at the ORM layer so it holds on SQLite and Postgres alike;
production adds Postgres triggers + REVOKE for defense in depth.
"""
from __future__ import annotations

import json
from datetime import datetime

from flask import g, has_request_context, request
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(db.ForeignKey("user.id"))
    user_name: Mapped[str | None] = mapped_column(db.String(120))
    branch_id: Mapped[int | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(db.String(80), index=True)
    entity: Mapped[str | None] = mapped_column(db.String(80))
    entity_id: Mapped[str | None] = mapped_column(db.String(40))
    old_value: Mapped[str | None] = mapped_column(db.Text)
    new_value: Mapped[str | None] = mapped_column(db.Text)
    ip_address: Mapped[str | None] = mapped_column(db.String(64))

    user = db.relationship("User", foreign_keys=[user_id])


class ImmutableRecordError(Exception):
    """Raised when code tries to change a record the spec forbids changing."""


def record(action, entity=None, entity_id=None, old=None, new=None, branch_id=None):
    """Append one audit entry. Never updates or deletes an existing one."""
    uid = uname = ip = None
    if has_request_context():
        ip = request.remote_addr
        if current_user and current_user.is_authenticated:
            uid = current_user.id
            uname = getattr(current_user, "full_name", None) or current_user.username
        branch_id = branch_id or getattr(g, "current_branch_id", None)

    entry = AuditLog(
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        old_value=_dump(old),
        new_value=_dump(new),
        user_id=uid,
        user_name=uname,
        branch_id=branch_id,
        ip_address=ip,
    )
    db.session.add(entry)
    return entry


def _dump(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def register_guards(app):
    """Install ORM-level immutability guards."""

    @event.listens_for(Session, "before_flush")
    def _block_forbidden_writes(session, flush_context, instances):
        # Audit rows: never update, never delete.
        for obj in session.dirty:
            if isinstance(obj, AuditLog):
                raise ImmutableRecordError("سجل التدقيق للقراءة فقط ولا يمكن تعديله.")
        for obj in session.deleted:
            if isinstance(obj, AuditLog):
                raise ImmutableRecordError("سجل التدقيق للقراءة فقط ولا يمكن حذفه.")

            # Posted immutable documents: never delete.
            if getattr(obj, "__immutable_when_posted__", False):
                if getattr(obj, "status", None) == "posted":
                    raise ImmutableRecordError(
                        "المستند المرحّل لا يُحذف. التصحيح بقيد عكسي."
                    )

        # Posted immutable documents: never update (except the very act of
        # posting, i.e. status transitioning INTO 'posted' in this flush).
        for obj in session.dirty:
            if not getattr(obj, "__immutable_when_posted__", False):
                continue
            if getattr(obj, "status", None) != "posted":
                continue
            status_hist = db.inspect(obj).attrs.status.history
            just_posted = "posted" in status_hist.added
            if not just_posted:
                raise ImmutableRecordError(
                    "القيد المرحّل لا يُعدَّل. التصحيح بقيد عكسي مرتبط بالأصلي."
                )

        # Lines of a posted entry are immutable too.
        from app.accounting.models import JournalLine

        for obj in list(session.dirty) + list(session.deleted):
            if isinstance(obj, JournalLine) and obj.entry is not None:
                if obj.entry.status == "posted" and obj not in session.new:
                    # allow when the parent entry is being posted in this flush
                    parent_hist = db.inspect(obj.entry).attrs.status.history
                    if "posted" not in parent_hist.added:
                        raise ImmutableRecordError(
                            "لا يمكن تعديل أطراف قيد مرحّل. التصحيح بقيد عكسي."
                        )
