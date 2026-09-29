"""Phase 1 — accounting kernel guarantees (spec §13)."""
from datetime import date
from decimal import Decimal

import pytest

from app import create_app
from app.extensions import db
from config import TestConfig


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        _seed()
        yield app
        db.session.remove()
        db.drop_all()


def _seed():
    from app.accounting.models import Account, AccountingPeriod
    from app.auth.models import Role, User
    # two postable accounts
    db.session.add_all([
        Account(code="1101", name_ar="الخزينة", type="asset", is_postable=True),
        Account(code="4101", name_ar="المبيعات", type="revenue", is_postable=True),
        Account(code="1301", name_ar="المخزون", type="asset", is_postable=True),
    ])
    db.session.add(AccountingPeriod(
        name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        status="open"))
    # two users to test separation of duties
    for uname in ("u1", "u2"):
        u = User(username=uname, full_name=uname)
        u.set_password("x")
        db.session.add(u)
    db.session.commit()


def _acc(code):
    from app.accounting.models import Account
    return db.session.scalar(db.select(Account).filter_by(code=code)).id


def _user(name):
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username=name)).id


# --- balance enforcement --------------------------------------------------
def test_balanced_entry_posts(app):
    from app.accounting import posting
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="100", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="100", side="credit"),
        ],
    )
    posting.post_entry(e, user_id=_user("u2"))
    db.session.commit()
    assert e.status == "posted"
    assert e.number and e.number.startswith("JE-")
    assert e.total_debit_book == Decimal("100.0000")


def test_unbalanced_entry_rejected(app):
    from app.accounting import posting
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="100", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="90", side="credit"),
        ],
    )
    with pytest.raises(posting.UnbalancedEntryError):
        posting.post_entry(e, user_id=_user("u2"))


# --- separation of duties (spec §3.2) -------------------------------------
def test_creator_cannot_post_own_manual_entry(app):
    from app.accounting import posting
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="50", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="50", side="credit"),
        ],
    )
    with pytest.raises(posting.SeparationOfDutiesError):
        posting.post_entry(e, user_id=_user("u1"))  # same user who created it


# --- closed period (spec §13.6) -------------------------------------------
def test_cannot_post_into_closed_period(app):
    from app.accounting import posting, services
    from app.accounting.models import AccountingPeriod
    period = db.session.scalar(db.select(AccountingPeriod))
    services.close_period(period, user_id=_user("u1"), approved_by_id=_user("u1"))
    db.session.commit()
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="10", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="10", side="credit"),
        ],
    )
    with pytest.raises(posting.ClosedPeriodError):
        posting.post_entry(e, user_id=_user("u2"))


# --- immutability (spec §13.5) --------------------------------------------
def test_posted_entry_cannot_be_edited(app):
    from app.accounting import posting
    from app.core.audit import ImmutableRecordError
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="10", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="10", side="credit"),
        ],
    )
    posting.post_entry(e, user_id=_user("u2"))
    db.session.commit()
    e.memo = "tampered"
    with pytest.raises(ImmutableRecordError):
        db.session.commit()
    db.session.rollback()


# --- reversal (spec §13.5) ------------------------------------------------
def test_reversal_swaps_and_links(app):
    from app.accounting import posting
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1101"), currency="AED", amount="100", side="debit"),
            posting.line(_acc("4101"), currency="AED", amount="100", side="credit"),
        ],
    )
    posting.post_entry(e, user_id=_user("u2"))
    db.session.commit()
    rev = posting.reverse_entry(e, user_id=_user("u2"))
    db.session.commit()
    assert rev.reverses_entry_id == e.id
    # debit/credit swapped
    orig_debit_acc = e.lines[0].account_id
    assert rev.lines[0].credit_book == e.lines[0].debit_book
    # net effect is zero across the two entries
    from app.accounting.services import trial_balance
    tb = trial_balance()
    assert tb["balanced"]
    assert all(r["balance"] == Decimal("0.0000") for r in tb["rows"])


# --- multi-currency book conversion (spec §5.2) ---------------------------
def test_foreign_currency_line_converts_to_book(app):
    from app.accounting import posting
    # 100 EGP at rate 0.08 -> 8 AED book
    e = posting.build_entry(
        entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
        lines=[
            posting.line(_acc("1301"), currency="EGP", amount="100", side="debit", fx_rate="0.08"),
            posting.line(_acc("4101"), currency="AED", amount="8", side="credit"),
        ],
    )
    assert e.lines[0].debit_book == Decimal("8.0000")
    posting.post_entry(e, user_id=_user("u2"))
    db.session.commit()
    assert e.is_balanced


# --- FX rate required (spec §16.19) ---------------------------------------
def test_missing_rate_blocks_operation(app):
    from app.core import fx
    with pytest.raises(fx.RateUnavailableError):
        fx.require_rate("EGP", "AED")  # no rate seeded, no manual value


def test_numbering_is_sequential(app):
    from app.accounting import posting
    numbers = []
    for _ in range(3):
        e = posting.build_entry(
            entry_date=date(2026, 3, 1), branch_id=None, user_id=_user("u1"),
            lines=[
                posting.line(_acc("1101"), currency="AED", amount="5", side="debit"),
                posting.line(_acc("4101"), currency="AED", amount="5", side="credit"),
            ],
        )
        posting.post_entry(e, user_id=_user("u2"))
        db.session.commit()
        numbers.append(e.number)
    seqs = [int(n.split("-")[-1]) for n in numbers]
    assert seqs == [seqs[0], seqs[0] + 1, seqs[0] + 2]
