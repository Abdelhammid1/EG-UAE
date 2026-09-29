"""Phase 2.5 — the vertical slice: cash sale ties revenue + COGS + cash + stock."""
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

    db.session.add(Currency(code="AED", name_ar="درهم", name_en="AED", decimal_places=2))
    chart = {
        "11": ("النقدية", "asset", False), "1301": ("المخزون", "asset", True),
        "3103": ("رصيد افتتاحي", "equity", True), "4101": ("إيراد المبيعات", "revenue", True),
        "5101": ("تكلفة المبيعات", "expense", True),
    }
    obj = {}
    for code, (n, t, pos) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=pos)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("sales.revenue", "4101"), ("cogs", "5101")]:
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
    p = Product(name_ar="هاتف", default_price="3000", min_price="2500",
                valuation_method="FIFO")
    db.session.add(p); db.session.flush()
    inv.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="2000", user_id=u.id)
    drawer = tr.create_treasury(name_ar="درج", branch_id=b.id, currency_code="AED",
                                type="drawer", opening_balance="0", user_id=u.id)
    db.session.commit()


def _ctx():
    from app.inventory.models import Product, Warehouse
    from app.treasury.models import Treasury
    from app.auth.models import User
    return (
        db.session.scalar(db.select(Product)),
        db.session.scalar(db.select(Warehouse)),
        db.session.scalar(db.select(Treasury)),
        db.session.scalar(db.select(User)).id,
    )


def test_opening_stock_on_hand(app):
    from app.inventory import services
    p, wh, _, _ = _ctx()
    assert services.qty_on_hand(p.id, wh.id) == Decimal("10.0000")


def test_cash_sale_full_chain(app):
    """One sale must move stock, compute COGS, credit revenue, and put cash in
    the drawer — all balanced."""
    from app.inventory import services
    from app.treasury import services as tr
    from app.accounting.services import trial_balance
    p, wh, drawer, uid = _ctx()

    sale = services.cash_sale(warehouse=wh, treasury=drawer,
                              lines=[(p, "2", "3000")], user_id=uid)
    db.session.commit()

    # stock down 10 -> 8
    assert services.qty_on_hand(p.id, wh.id) == Decimal("8.0000")
    # revenue 2*3000 = 6000, COGS 2*2000 = 4000, profit 2000
    assert sale.total == Decimal("6000.0000")
    assert sale.total_cost == Decimal("4000.0000")
    assert sale.profit == Decimal("2000.0000")
    # cash landed in the drawer
    assert tr.balance_native(drawer.account_id) == Decimal("6000.0000")
    # the sale posted a balanced journal entry
    assert sale.journal_entry.is_balanced
    # whole ledger balances
    assert trial_balance()["balanced"]


def test_min_price_blocks_sale(app):
    from app.inventory import services
    p, wh, drawer, uid = _ctx()
    with pytest.raises(services.MinPriceViolation):
        services.cash_sale(warehouse=wh, treasury=drawer,
                           lines=[(p, "1", "2000")], user_id=uid)  # below 2500


def test_below_min_allowed_with_flag(app):
    from app.inventory import services
    from app.core.audit import AuditLog
    p, wh, drawer, uid = _ctx()
    sale = services.cash_sale(warehouse=wh, treasury=drawer,
                              lines=[(p, "1", "2000")], user_id=uid,
                              allow_below_min=True)
    db.session.commit()
    assert sale.below_min_used is True
    # it was recorded in the audit log (§7.2)
    assert db.session.scalar(db.select(db.func.count(AuditLog.id))
                             .filter_by(action="sale.below_min")) == 1


def test_out_of_stock_blocks(app):
    from app.inventory import services
    p, wh, drawer, uid = _ctx()
    with pytest.raises(services.OutOfStockError):
        services.cash_sale(warehouse=wh, treasury=drawer,
                           lines=[(p, "999", "3000")], user_id=uid)


def test_fifo_cost_across_batches(app):
    """A second batch at a higher cost; selling 12 consumes 10@2000 + 2@2500."""
    from app.inventory import services
    from app.inventory.models import Product, Warehouse
    p, wh, drawer, uid = _ctx()
    services.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="2500",
                           user_id=uid)
    db.session.commit()
    sale = services.cash_sale(warehouse=wh, treasury=drawer,
                              lines=[(p, "12", "3000")], user_id=uid)
    db.session.commit()
    # COGS = 10*2000 + 2*2500 = 25000
    assert sale.total_cost == Decimal("25000.0000")
    assert services.qty_on_hand(p.id, wh.id) == Decimal("8.0000")  # 20 - 12
