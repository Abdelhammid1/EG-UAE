"""Per-branch, per-document-type sequential numbering (spec §4.1, §16.22).

The sequence row is locked FOR UPDATE inside the caller's transaction, so two
concurrent documents can never receive the same number. On SQLite (dev) writes
are already serialized; on PostgreSQL (prod) the row lock does the real work.

The *display format* is configurable from Settings (شاشة الترقيم): a template
such as ``INV-{br}-{seq}`` per document family, with ``{seq}`` width also
configurable. Placeholders: ``{pfx}`` (given prefix), ``{br}`` (2-digit branch),
``{seq}`` (zero-padded running number), ``{yy}`` / ``{yyyy}`` / ``{mm}`` (today).
Unknown or missing formats fall back to the classic ``{pfx}{br}-{seq}``.
"""
from __future__ import annotations

from datetime import date

from app.core import settings
from app.core.models import DocumentSequence
from app.extensions import db

# doc_type -> settings key suffix under "numbering."
_FMT_KEYS = {
    "SALE": "sale", "PINV": "purchase", "JE": "journal",
}


def _format_for(doc_type: str) -> str | None:
    key = _FMT_KEYS.get(doc_type)
    if not key:
        return None
    try:
        return settings.get(f"numbering.{key}") or None
    except settings.SettingNotFoundError:
        return None


def _seq_width(default: int = 6) -> int:
    try:
        w = settings.get("numbering.seq_width")
        return int(w) if w else default
    except (settings.SettingNotFoundError, ValueError, TypeError):
        return default


def next_number(branch_id: int | None, doc_type: str, prefix: str | None = None) -> str:
    """Reserve and return the next formatted number for (branch, doc_type).

    Must be called inside a transaction that also writes the document, so the
    reserved number is committed atomically with its use.
    """
    branch_key = branch_id or 0
    q = (
        db.select(DocumentSequence)
        .filter_by(branch_id=branch_key, doc_type=doc_type)
        .with_for_update()
    )
    seq = db.session.scalar(q)
    if seq is None:
        seq = DocumentSequence(
            branch_id=branch_key, doc_type=doc_type, prefix=prefix or "", next_number=1
        )
        db.session.add(seq)
        db.session.flush()

    number = seq.next_number
    seq.next_number = number + 1
    db.session.flush()

    pfx = (prefix if prefix is not None else seq.prefix) or ""
    branch_part = f"{branch_key:02d}"
    width = _seq_width()
    seq_part = f"{number:0{width}d}"
    today = date.today()

    fmt = _format_for(doc_type)
    if fmt:
        try:
            return fmt.format(pfx=pfx, br=branch_part, seq=seq_part,
                              yy=today.strftime("%y"), yyyy=today.year,
                              mm=today.strftime("%m"))
        except (KeyError, IndexError, ValueError):
            pass  # bad template -> fall back
    return f"{pfx}{branch_part}-{seq_part}"
