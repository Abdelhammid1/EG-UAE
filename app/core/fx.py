"""Exchange-rate service (spec §5.3, §5.4, §16.19).

A rate is suggested automatically and may be overridden manually before saving.
Every rate used is logged with its source and who entered it. If no rate is
available and none is entered, the operation cannot proceed.
"""
from __future__ import annotations

from datetime import datetime

from flask import current_app

from app.accounting.models import ExchangeRate
from app.core.money import quantize_rate, to_decimal
from app.extensions import db


class RateUnavailableError(Exception):
    """No stored rate and no manual value — the operation must not proceed."""


def get_rate(from_currency: str, to_currency: str, at: datetime | None = None):
    """Latest known rate at or before `at`. Same currency -> 1. None if unknown."""
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if from_currency == to_currency:
        return quantize_rate(1)

    at = at or datetime.utcnow()
    row = db.session.scalar(
        db.select(ExchangeRate)
        .filter_by(from_currency=from_currency, to_currency=to_currency)
        .filter(ExchangeRate.at <= at)
        # break timestamp ties by insertion order: the newest rate wins
        .order_by(ExchangeRate.at.desc(), ExchangeRate.id.desc())
    )
    return quantize_rate(row.rate) if row else None


def rate_to_book(from_currency: str, at: datetime | None = None):
    """Rate converting `from_currency` into the book currency (AED)."""
    return get_rate(from_currency, current_app.config["BOOK_CURRENCY"], at)


def require_rate(from_currency: str, to_currency: str, manual=None, at=None):
    """Return a usable rate or raise. Manual override wins (spec §5.3)."""
    if manual is not None and str(manual).strip() != "":
        return quantize_rate(manual)
    rate = get_rate(from_currency, to_currency, at)
    if rate is None:
        raise RateUnavailableError(
            f"لا يوجد سعر صرف من {from_currency} إلى {to_currency}. "
            "أدخل السعر يدويًا للمتابعة."
        )
    return rate


def record_rate(from_currency, to_currency, rate, *, source="manual",
                user_id=None, at=None):
    """Append a rate to the log (spec §5.3)."""
    row = ExchangeRate(
        from_currency=from_currency.upper(),
        to_currency=to_currency.upper(),
        rate=quantize_rate(rate),
        source=source,
        entered_by_id=user_id,
        at=at or datetime.utcnow(),
    )
    db.session.add(row)
    return row


def try_fetch_auto(from_currency, to_currency):
    """Best-effort automatic rate from an external source (spec §5.3).

    Returns a Decimal or None. Never blocks the operation — if it fails, the
    caller falls back to manual entry (§16.19). Configured via FX_API_URL.
    """
    url = current_app.config.get("FX_API_URL")
    if not url:
        return None
    try:
        import requests

        resp = requests.get(
            url, params={"base": from_currency, "symbols": to_currency}, timeout=4
        )
        resp.raise_for_status()
        data = resp.json()
        value = data.get("rates", {}).get(to_currency.upper())
        return to_decimal(value) if value is not None else None
    except Exception:
        return None
