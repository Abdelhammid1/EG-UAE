"""Gap-closing features: purchase return, damage, discount, user mgmt,
permission matrix, entity creation."""
from decimal import Decimal

import pytest

from app import create_app
from app.cli import run_seed
from app.extensions import db
from config import TestConfig


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        run_seed(create_tables=True)
        yield app
        db.session.remove()
        db.drop_all()


def _uid():
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username="owner")).id


def _wh():
    from app.inventory.models import Warehouse
    return db.session.scalar(db.select(Warehouse))


def _prod(bc="1001"):
    from app.inventory.models import Product
    return db.session.scalar(db.select(Product).filter_by(barcode=bc))


def _drawer():
    from app.treasury.models import Treasury
    return db.session.scalar(db.select(Treasury).filter_by(type="drawer"))


# --- purchase return (PUR-06) ---------------------------------------------
def test_purchase_return_reduces_stock_and_ap(app):
    from app.purchasing import services as pur
    from app.inventory import services as inv
    supplier = pur.create_supplier(name_ar="مورد", branch_id=1, user_id=_uid())
    db.session.commit()
    invoice = pur.create_purchase_invoice(supplier=supplier, warehouse=_wh(),
                                          lines=[(_prod(), "5", "2000")],
                                          payment_type="credit", user_id=_uid())
    db.session.commit()
    before = inv.qty_on_hand(_prod().id, _wh().id)
    assert pur.ap_balance(supplier) == Decimal("10000.0000")
    pur.create_purchase_return(invoice=invoice, user_id=_uid())
    db.session.commit()
    assert inv.qty_on_hand(_prod().id, _wh().id) == before - Decimal("5.0000")
    assert pur.ap_balance(supplier) == Decimal("0.0000")  # AP cleared


# --- damage / write-off (INV-07) ------------------------------------------
def test_write_off_reduces_stock_and_posts_loss(app):
    from app.inventory import services as inv
    from app.accounting.services import trial_balance
    before = inv.qty_on_hand(_prod().id, _wh().id)
    inv.write_off(product=_prod(), warehouse=_wh(), qty="2", reason="كسر", user_id=_uid())
    db.session.commit()
    assert inv.qty_on_hand(_prod().id, _wh().id) == before - Decimal("2.0000")
    assert trial_balance()["balanced"]


# --- invoice discount (SAL-01.4) ------------------------------------------
def test_sale_with_discount(app):
    from app.sales import services as s
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    # 2 x 3000 = 6000, discount 500 -> total 5500
    sale = s.make_sale(warehouse=_wh(), lines=[(_prod(), "2", "3000")],
                       payment_type="cash", treasury=drawer, shift=shift,
                       discount="500", user_id=_uid())
    db.session.commit()
    assert sale.subtotal == Decimal("5500.0000")  # after discount
    assert sale.total == Decimal("5500.0000")
    from app.treasury import services as tr
    assert tr.balance_native(drawer.account_id) == Decimal("5500.0000")


def test_discount_cannot_exceed_subtotal(app):
    from app.sales import services as s
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    with pytest.raises(ValueError):
        s.make_sale(warehouse=_wh(), lines=[(_prod(), "1", "3000")],
                    payment_type="cash", treasury=drawer, shift=shift,
                    discount="99999", user_id=_uid())


# --- user management + permission matrix (SET-02/03) ----------------------
def test_permission_change_takes_effect_next_request(app):
    """Editing the matrix changes what a user can do on the next check (SET-03.4)."""
    from app.auth.models import User, Role, Permission
    cashier = db.session.scalar(db.select(Role).filter_by(code="cashier"))
    u = User(username="c1", full_name="c"); u.set_password("x"); u.roles = [cashier]
    db.session.add(u); db.session.commit()
    assert u.has_permission("reports.financial") is False
    # grant the cashier role a new permission
    perm = db.session.scalar(db.select(Permission).filter_by(code="reports.financial"))
    cashier.permissions.append(perm)
    db.session.commit()
    # re-fetch the user (simulating the next request) -> now allowed
    u2 = db.session.scalar(db.select(User).filter_by(username="c1"))
    assert u2.has_permission("reports.financial") is True


def test_extra_permission_grant(app):
    from app.auth.models import User, Role, Permission
    cashier = db.session.scalar(db.select(Role).filter_by(code="cashier"))
    below = db.session.scalar(db.select(Permission).filter_by(code="sale.below_min"))
    u = User(username="c2", full_name="c"); u.set_password("x"); u.roles = [cashier]
    u.extra_permissions = [below]
    db.session.add(u); db.session.commit()
    assert u.has_permission("sale.below_min") is True  # granted individually
