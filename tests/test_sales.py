"""Phase 6 — customers, VAT, sales, shifts, returns (spec §10, §11)."""
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
    chart = {"11": ("نقدية", "asset", False), "1201": ("عملاء", "asset", False),
             "1301": ("مخزون", "asset", True), "2102": ("ضريبة", "liability", True),
             "3103": ("افتتاحي", "equity", True), "4101": ("مبيعات", "revenue", True),
             "4103": ("زيادة", "revenue", True), "5101": ("cogs", "expense", True),
             "5105": ("عجز", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("sales.revenue", "4101"), ("cogs", "5101"),
                     ("vat.output", "2102"), ("shift.shortage", "5105"),
                     ("shift.surplus", "4103"), ("cash.main", "11")]:
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
    p = Product(name_ar="هاتف", default_price="3000", min_price="2000",
                valuation_method="FIFO")
    db.session.add(p); db.session.flush()
    inv.receive_stock(product=p, warehouse=wh, qty="20", unit_cost="2000", user_id=u.id)
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


# --- shifts (spec §11) ----------------------------------------------------
def test_cash_sale_requires_open_shift(app):
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    with pytest.raises(s.NoOpenShiftError):
        s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="cash",
                    treasury=drawer, shift=None, user_id=uid)


def test_one_open_shift_per_drawer(app):
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="500", user_id=uid)
    db.session.commit()
    with pytest.raises(s.ShiftError):
        s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="0", user_id=uid)


def test_shift_close_computes_over_short(app):
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    shift = s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="500", user_id=uid)
    db.session.commit()
    s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="cash",
                treasury=drawer, shift=shift, user_id=uid)
    db.session.commit()
    # expected = 500 + 3000 = 3500 ; count 3450 -> shortage 50
    s.close_shift(shift=shift, counted_cash="3450", user_id=uid)
    db.session.commit()
    assert shift.expected_cash == Decimal("3500.0000")
    assert shift.over_short == Decimal("-50.0000")
    assert shift.journal_entry_id is not None


# --- VAT (spec §10.6) -----------------------------------------------------
def test_vat_applied_when_branch_enabled(app):
    from app.sales import services as s
    from app.core import settings
    from app.admin.models import SettingDefinition
    p, wh, drawer, uid, bid = _ctx()
    db.session.add(SettingDefinition(key="finance.tax_enabled", section="finance",
                                     value_type="bool", scope="branch",
                                     label_ar="ض", label_en="tax", default_value="false"))
    db.session.add(SettingDefinition(key="finance.tax_rate", section="finance",
                                     value_type="decimal", scope="branch",
                                     label_ar="نسبة", label_en="rate", default_value=None))
    db.session.flush()
    settings.set("finance.tax_enabled", "true", scope_id=bid, user_id=uid)
    settings.set("finance.tax_rate", "5", scope_id=bid, user_id=uid)
    shift = s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="0", user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="cash",
                       treasury=drawer, shift=shift, user_id=uid)
    db.session.commit()
    assert sale.subtotal == Decimal("3000.0000")
    assert sale.tax_amount == Decimal("150.0000")  # 5%
    assert sale.total == Decimal("3150.0000")


# --- credit sale + limit (spec §10.3) -------------------------------------
def test_credit_sale_and_limit(app):
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    cust = s.create_customer(name_ar="عميل", credit_limit="5000", branch_id=bid,
                             user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="credit",
                       customer=cust, user_id=uid)
    db.session.commit()
    assert s.ar_balance(cust) == Decimal("3000.0000")
    # a second 3000 sale would breach the 5000 limit
    with pytest.raises(s.CreditLimitError):
        s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="credit",
                    customer=cust, user_id=uid)


def test_collection_reduces_ar(app):
    from app.sales import services as s
    from app.accounting.models import Account
    p, wh, drawer, uid, bid = _ctx()
    cust = s.create_customer(name_ar="عميل", credit_limit="50000", branch_id=bid,
                             user_id=uid)
    db.session.commit()
    s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="credit",
                customer=cust, user_id=uid)
    db.session.commit()
    cash = db.session.scalar(db.select(Account).filter_by(code="11")).id
    s.collect(customer=cust, to_account_id=cash, amount="1000", branch_id=bid,
              user_id=uid)
    db.session.commit()
    assert s.ar_balance(cust) == Decimal("2000.0000")  # 3000 - 1000


# --- sales return (spec §10.7, §16.4) -------------------------------------
def test_sales_return_restores_stock_and_reverses(app):
    from app.sales import services as s
    from app.inventory import services as inv
    from app.accounting.services import trial_balance
    p, wh, drawer, uid, bid = _ctx()
    shift = s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="0", user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "2", "3000")], payment_type="cash",
                       treasury=drawer, shift=shift, user_id=uid)
    db.session.commit()
    assert inv.qty_on_hand(p.id, wh.id) == Decimal("18.0000")  # 20 - 2
    s.sales_return(sale=sale, user_id=uid)
    db.session.commit()
    assert inv.qty_on_hand(p.id, wh.id) == Decimal("20.0000")  # restored
    assert sale.returned is True
    assert trial_balance()["balanced"]


def test_cannot_return_twice(app):
    from app.sales import services as s
    p, wh, drawer, uid, bid = _ctx()
    shift = s.open_shift(cashier_id=uid, drawer=drawer, opening_cash="0", user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="cash",
                       treasury=drawer, shift=shift, user_id=uid)
    db.session.commit()
    s.sales_return(sale=sale, user_id=uid)
    db.session.commit()
    with pytest.raises(s.AlreadyReturnedError):
        s.sales_return(sale=sale, user_id=uid)
