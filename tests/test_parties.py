"""M4 — Parties with per-currency sub-accounts (PTY-02..09, scenario E6)."""
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
    from app.accounting.models import Account, AccountingPeriod, AccountMapping
    from app.core.models import Branch, Country, Currency
    from app.auth.models import User
    from app.treasury import services as tr

    for code, ar in [("AED", "درهم"), ("EGP", "جنيه"), ("SAR", "ريال")]:
        db.session.add(Currency(code=code, name_ar=ar, name_en=code, decimal_places=2))
    chart = {"11": ("نقدية", "asset", False), "1204": ("أرصدة لدى الأشخاص", "asset", False),
             "3103": ("افتتاحي", "equity", True), "4102": ("ربح فرق", "revenue", True),
             "5104": ("خسارة فرق", "expense", True), "5201": ("مصروف نقل", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("opening.equity", "3103"), ("fx.gain", "4102"),
                     ("fx.loss", "5104")]:
        db.session.add(AccountMapping(operation_code=op, branch_id=None,
                                      account_id=obj[code].id))
    for iso, name, cur in [("EG", "مصر", "EGP"), ("AE", "الإمارات", "AED")]:
        db.session.add(Country(name_ar=name, name_en=iso, iso_code=iso,
                               currency_code=cur))
    db.session.flush()
    from app.core.models import Country
    eg = db.session.scalar(db.select(Country).filter_by(iso_code="EG"))
    b = Branch(name_ar="مصر", name_en="EG", country_id=eg.id)
    db.session.add(b); db.session.flush()
    db.session.add(AccountingPeriod(name="2026", start_date=date(2026, 1, 1),
                                    end_date=date(2026, 12, 31), status="open"))
    u = User(username="u1", full_name="u1"); u.set_password("x")
    db.session.add(u); db.session.flush()
    # treasuries in EGP and SAR
    tr.create_treasury(name_ar="خزينة مصر", branch_id=b.id, currency_code="EGP",
                       opening_balance="100000", user_id=u.id)
    tr.create_treasury(name_ar="خزينة ريال", branch_id=b.id, currency_code="SAR",
                       opening_balance="50000", user_id=u.id)
    db.session.commit()


def _ctx():
    from app.treasury.models import Treasury
    from app.auth.models import User
    from app.core.models import Branch
    egp = db.session.scalar(db.select(Treasury).filter_by(currency_code="EGP"))
    sar = db.session.scalar(db.select(Treasury).filter_by(currency_code="SAR"))
    return egp, sar, db.session.scalar(db.select(User)).id, \
        db.session.scalar(db.select(Branch)).id


def _party(uid, bid):
    from app.parties import services as s
    p = s.create_party(name_ar="طرف", branch_id=bid, user_id=uid)
    db.session.commit()
    return p


# --- per-currency sub-accounts (PTY-02) -----------------------------------
def test_same_currency_pay(app):
    from app.parties import services as s
    egp, sar, uid, bid = _ctx()
    p = _party(uid, bid)
    s.pay_to_party(party=p, from_treasury=egp, amount="5000", user_id=uid)
    db.session.commit()
    assert s.party_balance(p, "EGP") == Decimal("5000.0000")
    from app.treasury.services import balance_native
    assert balance_native(egp.account_id) == Decimal("95000.0000")


def test_multi_currency_sub_accounts(app):
    """A party holds EGP + SAR at once, each its own sub-account (PTY-02)."""
    from app.parties import services as s
    egp, sar, uid, bid = _ctx()
    p = _party(uid, bid)
    s.pay_to_party(party=p, from_treasury=egp, amount="5000", user_id=uid)
    s.pay_to_party(party=p, from_treasury=sar, amount="2000", user_id=uid)
    db.session.commit()
    assert s.party_balance(p, "EGP") == Decimal("5000.0000")
    assert s.party_balance(p, "SAR") == Decimal("2000.0000")
    assert len(p.accounts) == 2


# --- cross-currency pay (PTY-04, scenario E6) -----------------------------
def test_pay_egp_recorded_in_aed(app):
    """Give EGP from the till, record with the party in AED at a rate."""
    from app.parties import services as s
    egp, sar, uid, bid = _ctx()
    p = _party(uid, bid)
    # send 5000 EGP, record in AED at 0.08 -> party AED balance 400
    s.pay_to_party(party=p, from_treasury=egp, amount="5000",
                   party_currency="AED", rate="0.08", user_id=uid)
    db.session.commit()
    assert s.party_balance(p, "AED") == Decimal("400.0000")
    from app.accounting.services import trial_balance
    assert trial_balance()["balanced"]


def test_refund_over_balance_blocked(app):
    from app.parties import services as s
    egp, sar, uid, bid = _ctx()
    p = _party(uid, bid)
    s.pay_to_party(party=p, from_treasury=egp, amount="1000", user_id=uid)
    db.session.commit()
    with pytest.raises(s.InsufficientPartyBalance):
        s.refund_from_party(party=p, to_treasury=egp, amount="2000", user_id=uid)


# --- PTY-09 summary invariant ---------------------------------------------
def test_summary_took_returned_spent_remaining(app):
    from app.parties import services as s
    from app.accounting.models import Account
    egp, sar, uid, bid = _ctx()
    p = _party(uid, bid)
    s.pay_to_party(party=p, from_treasury=egp, amount="5000", user_id=uid)
    s.refund_from_party(party=p, to_treasury=egp, amount="1000", user_id=uid)
    exp = db.session.scalar(db.select(Account).filter_by(code="5201")).id
    s.party_spend(party=p, expense_account_id=exp, amount="1500", currency="EGP",
                  user_id=uid)
    db.session.commit()
    summ = {r["currency"]: r for r in s.party_summary(p)}["EGP"]
    assert summ["took"] == Decimal("5000.0000")
    assert summ["returned"] == Decimal("1000.0000")
    assert summ["spent"] == Decimal("1500.0000")
    assert summ["remaining"] == Decimal("2500.0000")  # 5000 - 1000 - 1500
    # invariant: remaining == sub-account balance
    assert summ["remaining"] == s.party_balance(p, "EGP")


# --- PTY-08 where is money ------------------------------------------------
def test_where_is_money_aggregates(app):
    from app.parties import services as s
    from app.core import fx
    egp, sar, uid, bid = _ctx()
    fx.record_rate("EGP", "AED", "0.08", user_id=uid)
    fx.record_rate("SAR", "AED", "0.98", user_id=uid)
    p = _party(uid, bid)
    s.pay_to_party(party=p, from_treasury=egp, amount="5000", user_id=uid)
    db.session.commit()
    data = s.where_is_money(display_currency="AED")
    egp_row = next(r for r in data["rows"] if r["currency"] == "EGP")
    # EGP total = treasury 95000 + party 5000 = 100000
    assert egp_row["total"] == Decimal("100000.0000")
    assert egp_row["treasuries"] == Decimal("95000.0000")
    assert egp_row["parties"] == Decimal("5000.0000")
