"""Phase 5 — inter-warehouse transfers & landed cost (spec §8)."""
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
    chart = {"11": ("نقدية", "asset", False), "1301": ("مخزون", "asset", True),
             "1302": ("بضاعة في الطريق", "asset", True), "3103": ("افتتاحي", "equity", True),
             "5106": ("خسارة جرد", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("goods_in_transit", "1302"), ("inventory.adjust.loss", "5106")]:
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
    w1 = Warehouse(name_ar="مخزن أ", branch_id=b.id)
    w2 = Warehouse(name_ar="مخزن ب", branch_id=b.id)
    db.session.add_all([w1, w2]); db.session.flush()
    # two products: phone 3000, phone 1000 (the §8 example)
    p1 = Product(name_ar="هاتف غالي", default_price="4000", valuation_method="FIFO")
    p2 = Product(name_ar="هاتف رخيص", default_price="1500", valuation_method="FIFO")
    db.session.add_all([p1, p2]); db.session.flush()
    inv.receive_stock(product=p1, warehouse=w1, qty="1", unit_cost="3000", user_id=u.id)
    inv.receive_stock(product=p2, warehouse=w1, qty="1", unit_cost="1000", user_id=u.id)
    db.session.commit()


def _ctx():
    from app.inventory.models import Product, Warehouse
    from app.auth.models import User
    ws = db.session.scalars(db.select(Warehouse).order_by(Warehouse.id)).all()
    ps = db.session.scalars(db.select(Product).order_by(Product.id)).all()
    return ws[0], ws[1], ps[0], ps[1], db.session.scalar(db.select(User)).id


# --- the §8 worked example: 200 shipping -> 150 / 50 ----------------------
def test_landed_cost_allocation_by_value(app):
    from app.shipping import services as sh
    from app.inventory import services as inv
    w1, w2, p1, p2, uid = _ctx()
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "1"), (p2, "1")],
                              allocation_method="value", fx_rate="1", user_id=uid)
    db.session.commit()
    sh.send_permit(permit, user_id=uid)
    db.session.commit()
    # goods value 3000 + 1000 = 4000 in goods-in-transit
    assert permit.goods_book == Decimal("4000.0000")
    # add 200 shipping, paid from... use the equity account as a stand-in source
    from app.accounting.models import Account
    src = db.session.scalar(db.select(Account).filter_by(code="11")).id
    sh.add_expense(permit, kind="shipping", amount_book="200",
                   paid_from_account_id=src, user_id=uid)
    db.session.commit()
    # by value: 3000/4000*200 = 150 ; 1000/4000*200 = 50 (spec §8 example)
    lines = {l.product_id: l for l in permit.lines}
    assert lines[p1.id].alloc_shipping_book == Decimal("150.0000")
    assert lines[p2.id].alloc_shipping_book == Decimal("50.0000")
    # final unit cost = original + share
    assert lines[p1.id].final_unit_cost_book == Decimal("3150.0000")
    assert lines[p2.id].final_unit_cost_book == Decimal("1050.0000")


def test_receive_lands_stock_and_clears_transit(app):
    from app.shipping import services as sh
    from app.inventory import services as inv
    from app.accounting.services import trial_balance
    from app.accounting.models import Account
    w1, w2, p1, p2, uid = _ctx()
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "1"), (p2, "1")], fx_rate="1", user_id=uid)
    db.session.commit()
    sh.send_permit(permit, user_id=uid)
    src = db.session.scalar(db.select(Account).filter_by(code="11")).id
    sh.add_expense(permit, kind="shipping", amount_book="200",
                   paid_from_account_id=src, user_id=uid)
    db.session.commit()
    # source warehouse emptied
    assert inv.qty_on_hand(p1.id, w1.id) == Decimal("0.0000")
    sh.receive_permit(permit, user_id=uid)
    db.session.commit()
    # destination has the stock at landed cost
    assert inv.qty_on_hand(p1.id, w2.id) == Decimal("1.0000")
    assert inv.stock_value_book(p1.id, w2.id) == Decimal("3150.0000")
    # goods-in-transit account is back to zero
    git = db.session.scalar(db.select(Account).filter_by(code="1302"))
    from app.treasury.services import balance_native
    assert balance_native(git.id) == Decimal("0.0000")
    assert trial_balance()["balanced"]


def test_allocation_by_quantity(app):
    from app.shipping import services as sh
    from app.accounting.models import Account
    w1, w2, p1, p2, uid = _ctx()
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "1"), (p2, "1")],
                              allocation_method="quantity", fx_rate="1", user_id=uid)
    db.session.commit()
    sh.send_permit(permit, user_id=uid)
    src = db.session.scalar(db.select(Account).filter_by(code="11")).id
    sh.add_expense(permit, kind="shipping", amount_book="200",
                   paid_from_account_id=src, user_id=uid)
    db.session.commit()
    # equal quantities -> 100 / 100
    lines = {l.product_id: l for l in permit.lines}
    assert lines[p1.id].alloc_shipping_book == Decimal("100.0000")
    assert lines[p2.id].alloc_shipping_book == Decimal("100.0000")


def test_shortfall_booked_as_loss(app):
    from app.shipping import services as sh
    from app.inventory import services as inv
    from app.accounting.services import trial_balance
    w1, w2, p1, p2, uid = _ctx()
    # send 1 + 1, receive only p1 (p2 lost in transit)
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "1"), (p2, "1")], fx_rate="1", user_id=uid)
    db.session.commit()
    sh.send_permit(permit, user_id=uid)
    db.session.commit()
    lines = {l.product_id: l for l in permit.lines}
    sh.receive_permit(permit, received={lines[p1.id].id: "1", lines[p2.id].id: "0"},
                      user_id=uid)
    db.session.commit()
    assert inv.qty_on_hand(p2.id, w2.id) == Decimal("0.0000")
    assert trial_balance()["balanced"]  # loss cleared the transit account


def test_cannot_send_more_than_available(app):
    from app.shipping import services as sh
    w1, w2, p1, p2, uid = _ctx()
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "5")], fx_rate="1", user_id=uid)
    db.session.commit()
    with pytest.raises(sh.NotEnoughStockError):
        sh.send_permit(permit, user_id=uid)


def test_cancel_only_before_send(app):
    from app.shipping import services as sh
    w1, w2, p1, p2, uid = _ctx()
    permit = sh.create_permit(from_warehouse=w1, to_warehouse=w2,
                              lines=[(p1, "1")], fx_rate="1", user_id=uid)
    db.session.commit()
    sh.send_permit(permit, user_id=uid)
    db.session.commit()
    with pytest.raises(sh.TransferStateError):
        sh.cancel_permit(permit, user_id=uid)
