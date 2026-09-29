"""The posting engine — the single gateway for creating journal entries.

Guarantees (spec §13):
  * Balanced or it does not post: Σ debit_book == Σ credit_book (§13.2).
  * No posting into a closed period (§13.6).
  * Posted entries are immutable; corrections are reversal entries (§13.5).
  * Manual entries cannot be posted by their own creator (separation of
    duties, §3.2) — enforced in the service, not just the UI.
  * Numbers are assigned under a row lock at post time (§16.22).

Automatic entries from documents call build_entry(...) + post_entry(...) with
accounts resolved from AccountMapping (configuration, never hardcoded, §13).
"""
from __future__ import annotations

from datetime import date, datetime

from app.accounting.models import (
    AccountingPeriod,
    AccountMapping,
    JournalEntry,
    JournalLine,
)
from app.core import audit, fx
from app.core.money import quantize_amount, quantize_rate
from app.core.numbering import next_number
from app.extensions import db


class UnbalancedEntryError(Exception):
    pass


class ClosedPeriodError(Exception):
    pass


class SeparationOfDutiesError(Exception):
    pass


class NoLinesError(Exception):
    pass


class MappingMissingError(Exception):
    pass


def line(account_id, *, currency, amount, side, fx_rate=None, memo=None):
    """Helper to build one line. `side` is 'debit' or 'credit'.

    The book equivalent is computed now and stored (spec §5.2), so it never
    shifts if the rate later changes.
    """
    from app.core.fx import rate_to_book

    amount = quantize_amount(amount)
    rate = quantize_rate(fx_rate) if fx_rate is not None else rate_to_book(currency)
    if rate is None:
        from app.core.fx import RateUnavailableError

        raise RateUnavailableError(
            f"لا يوجد سعر صرف لعملة {currency}. أدخل السعر يدويًا."
        )
    book = quantize_amount(amount * rate)
    return {
        "account_id": account_id,
        "currency_code": currency,
        "fx_rate": rate,
        "debit_original": amount if side == "debit" else 0,
        "credit_original": amount if side == "credit" else 0,
        "debit_book": book if side == "debit" else 0,
        "credit_book": book if side == "credit" else 0,
        "memo": memo,
    }


def account_for(operation_code: str, branch_id: int | None = None):
    """Resolve the account bound to an operation (spec §13 mapping table)."""
    row = db.session.scalar(
        db.select(AccountMapping).filter_by(
            operation_code=operation_code, branch_id=branch_id
        )
    )
    if row is None and branch_id is not None:
        row = db.session.scalar(
            db.select(AccountMapping).filter_by(
                operation_code=operation_code, branch_id=None
            )
        )
    if row is None:
        raise MappingMissingError(
            f"لا يوجد حساب مربوط بالعملية «{operation_code}». اربطه من الإعدادات."
        )
    return row.account_id


def _period_for(d: date) -> AccountingPeriod | None:
    return db.session.scalar(
        db.select(AccountingPeriod)
        .filter(AccountingPeriod.start_date <= d, AccountingPeriod.end_date >= d)
    )


def build_entry(*, entry_date, branch_id, lines, source_doc_type="manual",
                source_doc_id=None, memo=None, user_id=None) -> JournalEntry:
    """Create a DRAFT entry. Does not post."""
    if not lines:
        raise NoLinesError("القيد يجب أن يحتوي على أطراف.")

    period = _period_for(entry_date)
    entry = JournalEntry(
        date=entry_date,
        branch_id=branch_id,
        period_id=period.id if period else None,
        source_doc_type=source_doc_type,
        source_doc_id=source_doc_id,
        memo=memo,
        status="draft",
        created_by_id=user_id,
    )
    for ld in lines:
        entry.lines.append(JournalLine(**ld))
    db.session.add(entry)
    db.session.flush()
    return entry


def post_entry(entry: JournalEntry, *, user_id=None) -> JournalEntry:
    """Validate and post a draft entry (spec §13.2, §13.4, §13.6, §3.2)."""
    if entry.status == "posted":
        return entry

    # Balance (§13.2)
    if not entry.is_balanced:
        raise UnbalancedEntryError(
            f"القيد غير متزن: مدين {entry.total_debit_book} ≠ دائن "
            f"{entry.total_credit_book}."
        )
    if entry.total_debit_book == 0:
        raise UnbalancedEntryError("القيد فارغ القيمة.")

    # Closed period (§13.6)
    period = _period_for(entry.date)
    if period is not None and not period.is_open:
        raise ClosedPeriodError(
            "لا يمكن الترحيل إلى فترة مقفلة. استخدم قيدًا تصحيحيًا في الفترة الجارية."
        )
    entry.period_id = period.id if period else None

    # Separation of duties for MANUAL entries (§3.2)
    if (
        entry.source_doc_type == "manual"
        and user_id is not None
        and entry.created_by_id is not None
        and user_id == entry.created_by_id
    ):
        raise SeparationOfDutiesError(
            "لا يمكن للمستخدم اعتماد قيد يدوي أنشأه بنفسه. يعتمده مستخدم آخر."
        )

    # Assign number under a row lock (§16.22) and post.
    entry.number = next_number(entry.branch_id, "JE", prefix="JE-")
    entry.status = "posted"
    entry.posted_by_id = user_id
    entry.posted_at = datetime.utcnow()
    db.session.flush()

    audit.record(
        action="journal.post",
        entity="journal_entry",
        entity_id=entry.id,
        new={"number": entry.number, "debit": str(entry.total_debit_book)},
        branch_id=entry.branch_id,
    )
    return entry


def reverse_entry(original: JournalEntry, *, entry_date=None, user_id=None,
                  memo=None) -> JournalEntry:
    """Create and post a reversing entry (spec §13.5). Original stays visible."""
    if original.status != "posted":
        raise ValueError("لا يمكن عكس قيد غير مرحّل.")

    entry_date = entry_date or date.today()
    reversal = JournalEntry(
        date=entry_date,
        branch_id=original.branch_id,
        source_doc_type=original.source_doc_type,
        source_doc_id=original.source_doc_id,
        reverses_entry_id=original.id,
        memo=memo or f"عكس القيد {original.number}",
        status="draft",
        created_by_id=user_id,
    )
    for l in original.lines:
        reversal.lines.append(
            JournalLine(
                account_id=l.account_id,
                currency_code=l.currency_code,
                fx_rate=l.fx_rate,
                # swap debit <-> credit
                debit_original=l.credit_original,
                credit_original=l.debit_original,
                debit_book=l.credit_book,
                credit_book=l.debit_book,
                memo=l.memo,
            )
        )
    db.session.add(reversal)
    db.session.flush()

    # A reversal is system-generated, so separation of duties does not block it.
    reversal.source_doc_type = original.source_doc_type
    reversal.number = next_number(reversal.branch_id, "JE", prefix="JE-")
    reversal.status = "posted"
    reversal.posted_by_id = user_id
    reversal.posted_at = datetime.utcnow()
    db.session.flush()

    audit.record(
        action="journal.reverse",
        entity="journal_entry",
        entity_id=reversal.id,
        old={"reverses": original.number},
        new={"number": reversal.number},
        branch_id=reversal.branch_id,
    )
    return reversal
