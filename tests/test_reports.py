"""Phase 7 — reports engine (spec §14)."""
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
    from app.inventory.models import Product, Warehouse
    from app.inventory import services as inv
    from app.treasury import services as tr

    for code, ar in [("AED", "درهم"), ("EGP", "جنيه")]:
        db.session.add(Currency(code=code, name_ar=ar, name_en=code, decimal_places=2))
    chart = {"11": ("نقدية", "asset", False), "1301": ("مخزون", "asset", True),
             "1201": ("عملاء", "asset", False),
             "3103": ("افتتاحي", "equity", True), "3101": ("رأس المال", "equity", True),
             "4101": ("مبيعات", "revenue", True), "4102": ("ربح فرق", "revenue", True),
             "5101": ("cogs", "expense", True), "5104": ("خسارة فرق", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("sales.revenue", "4101"), ("cogs", "5101"),
                     ("fx.gain", "4102"), ("fx.loss", "5104")]:
        db.session.add(AccountMapping(operation_code=op, branch_id=None,
                                      account_id=obj[code].id))
    c = Country(name_ar="الإمارات", name_en="UAE", iso_code="AE", currency_code="AED")
    db.session.add(c); db.session.flush()
    b = Branch(name_ar="دبي", name_en="Dubai", country_id=c.id)
    db.session.add(b); db.session.flush()
    db.session.add(AccountingPeriod(name="2026", start_date=date(2026, 1, 1),
                                    end_date=date(2026, 12, 31), status="open"))
    u = User(username="u1", full_name="u1"); u.set_password("x")
    db.session.add(u); db.session.flush()
    wh = Warehouse(name_ar="مخزن", branch_id=b.id); db.session.add(wh); db.session.flush()
    p = Product(name_ar="هاتف", default_price="3000", min_price="1000")
    db.session.add(p); db.session.flush()
    inv.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="2000", user_id=u.id)
    tr.create_treasury(name_ar="درج", branch_id=b.id, currency_code="AED",
                       type="drawer", opening_balance="0", user_id=u.id)
    db.session.commit()


def _ctx():
    from app.inventory.models import Product, Warehouse
    from app.treasury.models import Treasury
    from app.auth.models import User
    from app.core.models import Branch
    return (db.session.scalar(db.select(Product)),
            db.session.scalar(db.select(Warehouse)),
            db.session.scalar(db.select(Treasury)),
            db.session.scalar(db.select(User)).id,
            db.session.scalar(db.select(Branch)).id)


def _make_a_sale():
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    shift = s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="0", user_id=uid)
    db.session.commit()
    s.make_sale(warehouse=wh, lines=[(p, "2", "3000")], payment_type="cash",
                treasury=drawer, shift=shift, user_id=uid)
    db.session.commit()


def test_income_statement(app):
    from app.reports import services as r
    _make_a_sale()  # revenue 6000, cogs 4000
    inc = r.income_statement(start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert inc["revenue_total"] == Decimal("6000.0000")
    assert inc["cogs"] == Decimal("4000.0000")
    assert inc["gross"] == Decimal("2000.0000")
    assert inc["net"] == Decimal("2000.0000")


def test_balance_sheet_balances(app):
    from app.reports import services as r
    _make_a_sale()
    bs = r.balance_sheet(as_of=date(2026, 12, 31))
    assert bs["balanced"] is True
    # assets = liabilities + equity (incl net income)
    assert bs["assets_total"] == bs["liabilities_total"] + bs["equity_total"]


def test_sales_by_product(app):
    from app.reports import services as r
    _make_a_sale()
    rows = r.sales_by_product()
    assert len(rows) == 1
    assert rows[0]["revenue"] == Decimal("6000.0000")
    assert rows[0]["margin"] == Decimal("2000.0000")


def test_dashboard_aggregates(app):
    from app.reports import services as r
    _make_a_sale()
    d = r.dashboard()
    assert d["cash"] == Decimal("6000.0000")   # drawer got 6000
    assert d["inventory"] == Decimal("16000.0000")  # 20000 opening - 4000 sold
    assert d["net_income"] == Decimal("2000.0000")


def test_general_ledger_running_balance(app):
    from app.reports import services as r
    from app.accounting.models import Account
    _make_a_sale()
    revenue = db.session.scalar(db.select(Account).filter_by(code="4101"))
    rows = r.general_ledger(revenue.id, start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert rows[-1]["balance"] == Decimal("6000.0000")  # revenue credit balance


def test_unrealized_fx_revaluation_posts_and_reverses(app):
    from app.reports import services as r
    from app.treasury import services as tr
    from app.core import fx
    from app.accounting.models import AccountingPeriod, JournalEntry
    p, wh, drawer, uid, bid = _ctx()
    # an EGP treasury with a balance, and a rate that later moves
    fx.record_rate("EGP", "AED", "0.08", user_id=uid)
    egp = tr.create_treasury(name_ar="خزينة مصر", branch_id=bid, currency_code="EGP",
                             opening_balance="10000", user_id=uid)
    db.session.commit()
    # rate rises to 0.09 -> unrealized gain on the 10000 EGP
    fx.record_rate("EGP", "AED", "0.09", user_id=uid)
    db.session.commit()
    period = db.session.scalar(db.select(AccountingPeriod))
    entry = r.revalue_unrealized(period=period, user_id=uid)
    db.session.commit()
    assert entry is not None
    assert entry.is_balanced
    # a reversal exists for the next period
    rev = db.session.scalar(db.select(JournalEntry).filter_by(reverses_entry_id=entry.id))
    assert rev is not None
