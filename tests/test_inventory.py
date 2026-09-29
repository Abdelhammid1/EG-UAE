"""Phase 3 — inventory depth: WA valuation, serials, stocktake, reorder."""
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
    from app.inventory.models import Warehouse

    for code, ar in [("AED", "درهم"), ("EGP", "جنيه")]:
        db.session.add(Currency(code=code, name_ar=ar, name_en=code, decimal_places=2))
    chart = {"11": ("نقدية", "asset", False), "1301": ("المخزون", "asset", True),
             "3103": ("افتتاحي", "equity", True), "4101": ("مبيعات", "revenue", True),
             "5101": ("cogs", "expense", True), "4104": ("ربح جرد", "revenue", True),
             "5106": ("خسارة جرد", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("sales.revenue", "4101"), ("cogs", "5101"),
                     ("inventory.adjust.gain", "4104"), ("inventory.adjust.loss", "5106")]:
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
    db.session.add(Warehouse(name_ar="مخزن", branch_id=b.id))
    db.session.commit()


def _wh():
    from app.inventory.models import Warehouse
    return db.session.scalar(db.select(Warehouse))


def _mk(name="منتج", method="FIFO", serial=False, minp="0", reorder="0"):
    from app.inventory.models import Product
    p = Product(name_ar=name, default_price="100", min_price=minp,
                valuation_method=method, track_serial=serial, reorder_level=reorder)
    db.session.add(p); db.session.flush()
    return p


# --- weighted average -----------------------------------------------------
def test_weighted_average_cogs(app):
    from app.inventory import services as s
    wh = _wh()
    p = _mk(method="WA")
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="100")
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="200")
    db.session.commit()
    # WA cost = (10*100 + 10*200)/20 = 150; selling 5 -> COGS 750
    cogs = s.issue_stock(product=p, warehouse=wh, qty="5")
    assert cogs == Decimal("750.0000")


def test_fifo_cogs(app):
    from app.inventory import services as s
    wh = _wh()
    p = _mk(method="FIFO")
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="100")
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="200")
    db.session.commit()
    # FIFO: 12 units -> 10*100 + 2*200 = 1400
    cogs = s.issue_stock(product=p, warehouse=wh, qty="12")
    assert cogs == Decimal("1400.0000")


# --- serials (spec §16.14, §16.15) ----------------------------------------
def test_serial_received_and_cannot_duplicate(app):
    from app.inventory import services as s
    wh = _wh()
    p = _mk(serial=True)
    s.receive_stock(product=p, warehouse=wh, qty="2", unit_cost="500",
                    serials=["A1", "A2"])
    db.session.commit()
    assert s.qty_on_hand(p.id, wh.id) == Decimal("2.0000")
    with pytest.raises(s.SerialError):  # duplicate serial (§16.15)
        s.receive_stock(product=p, warehouse=wh, qty="1", unit_cost="500",
                        serials=["A1"])


def test_serial_cannot_be_sold_twice(app):
    from app.inventory import services as s
    from app.inventory.models import Warehouse
    from app.treasury import services as tr
    wh = _wh()
    p = _mk(serial=True, minp="0")
    s.receive_stock(product=p, warehouse=wh, qty="2", unit_cost="500",
                    serials=["A1", "A2"])
    drawer = tr.create_treasury(name_ar="درج", branch_id=wh.branch_id,
                                currency_code="AED", type="drawer")
    db.session.commit()
    s.cash_sale(warehouse=wh, treasury=drawer,
                lines=[(p, "1", "800", ["A1"])])
    db.session.commit()
    with pytest.raises(s.SerialError):  # A1 already sold (§16.14)
        s.cash_sale(warehouse=wh, treasury=drawer,
                    lines=[(p, "1", "800", ["A1"])])


# --- reorder --------------------------------------------------------------
def test_reorder_alert(app):
    from app.inventory import services as s
    wh = _wh()
    p = _mk(reorder="15")
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="100")
    db.session.commit()
    alerts = s.reorder_alerts()
    assert any(a["product"].id == p.id for a in alerts)  # 10 <= 15


# --- stocktake ------------------------------------------------------------
def test_stocktake_shortage_posts_loss(app):
    from app.inventory import services as s
    from app.auth.models import User
    from app.accounting.services import trial_balance
    wh = _wh()
    p = _mk()
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="100")
    db.session.commit()
    uid = db.session.scalar(db.select(User)).id
    st = s.open_stocktake(warehouse=wh, user_id=uid)
    # count only 8 -> shortage of 2 (value 200)
    s.save_counts(st, {p.id: "8"})
    s.approve_stocktake(st, user_id=uid, approver_id=uid)
    db.session.commit()
    assert s.qty_on_hand(p.id, wh.id) == Decimal("8.0000")
    assert st.status == "approved"
    assert st.journal_entry_id is not None
    assert trial_balance()["balanced"]


def test_stocktake_needs_approver(app):
    from app.inventory import services as s
    from app.auth.models import User
    wh = _wh()
    p = _mk()
    s.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="100")
    db.session.commit()
    uid = db.session.scalar(db.select(User)).id
    st = s.open_stocktake(warehouse=wh, user_id=uid)
    s.save_counts(st, {p.id: "8"})
    with pytest.raises(s.StocktakeError):
        s.approve_stocktake(st, user_id=uid, approver_id=None)
