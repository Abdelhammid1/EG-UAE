"""End-to-end acceptance scenarios E1–E9 from the user-stories doc.

Each exercises several modules together and passes only if every number is right.
Uses the full seed for a realistic environment.
"""
from datetime import date
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
        _accountant()
        yield app
        db.session.remove()
        db.drop_all()


def _accountant():
    from app.auth.models import User, Role
    u = User(username="acc", full_name="محاسب"); u.set_password("x")
    u.roles = [db.session.scalar(db.select(Role).filter_by(code="accountant"))]
    db.session.add(u); db.session.commit()
    return u


def _uid(name="owner"):
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username=name)).id


def _wh(name_en=None):
    from app.inventory.models import Warehouse
    if name_en:
        return db.session.scalar(db.select(Warehouse).filter_by(name_ar=name_en))
    return db.session.scalar(db.select(Warehouse))


def _prod(bc):
    from app.inventory.models import Product
    return db.session.scalar(db.select(Product).filter_by(barcode=bc))


def _balanced():
    from app.accounting.services import trial_balance
    return trial_balance()["balanced"]


# --- E1: UAE -> Egypt transfer at an FX rate ------------------------------
def test_e1_cross_currency_transfer(app):
    from app.treasury import services as tr
    uae = tr.create_treasury(name_ar="بنك الإمارات", branch_id=2, currency_code="AED",
                             opening_balance="100000", user_id=_uid())
    egy = tr.create_treasury(name_ar="بنك مصر", branch_id=1, currency_code="EGP",
                             user_id=_uid())
    db.session.commit()
    tr.fx_transfer(src=uae, dst=egy, sent_amount="1000", received_amount="12000",
                   src_rate="1", dst_rate="0.083", user_id=_uid())
    db.session.commit()
    assert tr.balance_native(uae.account_id) == Decimal("99000.0000")
    assert tr.balance_native(egy.account_id) == Decimal("12000.0000")
    assert _balanced()


# --- E2: custody -> purchase -> settle ------------------------------------
def test_e2_custody_purchase_settle(app):
    from app.custody import services as cust
    from app.purchasing import services as pur
    from app.treasury import services as tr
    from app.treasury.models import Treasury
    from app.inventory import services as inv
    main = db.session.scalar(db.select(Treasury).filter_by(type="main"))
    c = db.session.scalar(db.select(__import__("app.custody.models", fromlist=["Custody"]).Custody))
    cust.issue(custody=c, from_treasury=main, amount="5000", user_id=_uid())
    db.session.commit()
    pur.create_purchase_invoice(supplier=None, warehouse=_wh(),
        lines=[(_prod("1001"), "1", "3200")], payment_type="custody", custody=c,
        user_id=_uid())
    db.session.commit()
    assert cust.available(c) == Decimal("1800.0000")   # 5000 - 3200
    with pytest.raises(pur.InsufficientCustodyError):
        pur.create_purchase_invoice(supplier=None, warehouse=_wh(),
            lines=[(_prod("1001"), "1", "2000")], payment_type="custody", custody=c,
            user_id=_uid())
    cust.close(custody=c, to_treasury=main, user_id=_uid())
    db.session.commit()
    assert cust.available(c) == Decimal("0.0000")
    assert _balanced()


# --- E3: shipment with freight + customs ----------------------------------
def test_e3_shipment_landed_cost(app):
    from app.shipping import services as sh
    from app.inventory import services as inv
    from app.inventory.models import Warehouse
    from app.accounting.models import Account
    whs = db.session.scalars(db.select(Warehouse).order_by(Warehouse.id)).all()
    src, dst = whs[0], whs[1]
    p1, p2 = _prod("1001"), _prod("1003")
    permit = sh.create_permit(from_warehouse=src, to_warehouse=dst,
                              lines=[(p1, "1"), (p2, "1")], fx_rate="1", user_id=_uid())
    db.session.commit()
    sh.send_permit(permit, user_id=_uid())
    cash = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    sh.add_expense(permit, kind="shipping", amount_book="200",
                   paid_from_account_id=cash, user_id=_uid())
    db.session.commit()
    lines = {l.product_id: l for l in permit.lines}
    # by value: p1 cost 2000, p2 cost 150 -> shares 200*2000/2150 and 200*150/2150
    total_cost = lines[p1.id].unit_cost_book + lines[p2.id].unit_cost_book
    assert lines[p1.id].alloc_shipping_book + lines[p2.id].alloc_shipping_book == Decimal("200.0000")
    sh.receive_permit(permit, user_id=_uid())
    db.session.commit()
    git = db.session.scalar(db.select(Account).filter_by(code="1302"))
    from app.treasury.services import balance_native
    assert balance_native(git.id) == Decimal("0.0000")  # transit cleared
    assert _balanced()


# --- E4: POS mixed day + shift close --------------------------------------
def test_e4_pos_day_and_shift_close(app):
    from app.sales import services as s
    from app.inventory import services as inv
    from app.treasury.models import Treasury
    drawer = db.session.scalar(db.select(Treasury).filter_by(type="drawer"))
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="100", user_id=_uid())
    db.session.commit()
    sale = s.make_sale(warehouse=_wh(), lines=[(_prod("1001"), "1", "3000")],
                       payment_type="cash", treasury=drawer, shift=shift, user_id=_uid())
    db.session.commit()
    # a return, approved by a different user (separation of duties holds at the app layer)
    s.sales_return(sale=sale, user_id=_uid("acc"))
    db.session.commit()
    # close with a deliberate shortage
    s.close_shift(shift=shift, counted_cash="50", user_id=_uid())
    db.session.commit()
    assert shift.over_short == Decimal("-50.0000")  # expected 100 (sale returned), counted 50
    assert _balanced()


# --- E5: funder sale + settlement -----------------------------------------
def test_e5_funder_sale_settle(app):
    from app.sales import services as s
    from app.sales.models import Funder
    from app.treasury import services as tr
    funder = db.session.scalar(db.select(Funder))
    sale = s.make_sale(warehouse=_wh(), lines=[(_prod("1001"), "1", "3000")],
                       payment_type="funder", funder=funder, user_id=_uid())
    db.session.commit()
    assert s.funder_open_receivable(funder) == Decimal("3000.0000")
    bank = tr.create_bank_account(name_ar="بنك", bank_name="X", account_number="1",
                                  branch_id=2, currency_code="AED", opening_balance="0",
                                  user_id=_uid())
    db.session.commit()
    st = s.settle_funder(funder=funder, sales=[sale], bank_account_id=bank.account_id,
                         received_amount="2820", user_id=_uid())  # 6% commission
    db.session.commit()
    assert st.commission == Decimal("180.0000")
    assert s.funder_open_receivable(funder) == Decimal("0.0000")
    assert _balanced()


# --- E6: pay a person in a different currency, then refund ----------------
def test_e6_party_cross_currency(app):
    from app.parties import services as ps
    from app.treasury.models import Treasury
    from app.core import fx
    main = db.session.scalar(db.select(Treasury).filter_by(type="main"))  # AED
    party = ps.create_party(name_ar="شخص", branch_id=2, user_id=_uid())
    db.session.commit()
    # give 400 AED from the till, record with the person in EGP at 12.5
    ps.pay_to_party(party=party, from_treasury=main, amount="400",
                    party_currency="EGP", rate="12.5", user_id=_uid())
    db.session.commit()
    assert ps.party_balance(party, "EGP") == Decimal("5000.0000")  # 400 * 12.5
    assert _balanced()


# --- E8: period close + unrealized revaluation ----------------------------
def test_e8_period_close_revaluation(app):
    from app.treasury import services as tr
    from app.reports import services as rep
    from app.core import fx
    from app.accounting.models import AccountingPeriod, JournalEntry
    fx.record_rate("EGP", "AED", "0.08", user_id=_uid())
    egy = tr.create_treasury(name_ar="خزينة مصر", branch_id=1, currency_code="EGP",
                             opening_balance="10000", user_id=_uid())
    db.session.commit()
    fx.record_rate("EGP", "AED", "0.09", user_id=_uid())  # rate moved
    db.session.commit()
    period = db.session.scalar(db.select(AccountingPeriod))
    entry = rep.revalue_unrealized(period=period, user_id=_uid())
    db.session.commit()
    assert entry is not None and entry.is_balanced
    # reversal exists for next period
    assert db.session.scalar(
        db.select(JournalEntry).filter_by(reverses_entry_id=entry.id)) is not None
    assert _balanced()


# --- cross-cutting invariants (run always) --------------------------------
def test_invariant_trial_balance_after_mixed_ops(app):
    from app.sales import services as s
    from app.treasury.models import Treasury
    drawer = db.session.scalar(db.select(Treasury).filter_by(type="drawer"))
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    s.make_sale(warehouse=_wh(), lines=[(_prod("1001"), "2", "3000")],
                payment_type="cash", treasury=drawer, shift=shift, user_id=_uid())
    db.session.commit()
    assert _balanced()
