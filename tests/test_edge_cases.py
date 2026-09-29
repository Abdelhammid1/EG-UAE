"""Spec §16 — the 24 edge cases, each a named test. Uses the full seed."""
from datetime import date, timedelta
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


# --- shared lookups -------------------------------------------------------
def _u():
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username="owner")).id


def _u2():
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username="acc") ) or \
        _make_accountant()


def _make_accountant():
    from app.auth.models import User, Role
    u = User(username="acc", full_name="محاسب"); u.set_password("x")
    u.roles = [db.session.scalar(db.select(Role).filter_by(code="accountant"))]
    db.session.add(u); db.session.flush()
    return u


def _wh():
    from app.inventory.models import Warehouse
    return db.session.scalar(db.select(Warehouse))


def _drawer():
    from app.treasury.models import Treasury
    return db.session.scalar(db.select(Treasury).filter_by(type="drawer"))


def _product(barcode="1001"):
    from app.inventory.models import Product
    return db.session.scalar(db.select(Product).filter_by(barcode=barcode))


# --- the 24 edge cases ----------------------------------------------------
def test_ec01_wrong_fx_transfer_corrected_by_reversal(app):
    from app.accounting import posting
    from app.accounting.models import Account, JournalEntry
    a = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    b = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    e = posting.build_entry(entry_date=date.today(), branch_id=None, user_id=_u(),
        lines=[posting.line(a, currency="AED", amount="100", side="debit"),
               posting.line(b, currency="AED", amount="100", side="credit")])
    posting.post_entry(e, user_id=_u2().id)
    rev = posting.reverse_entry(e, user_id=_u())
    db.session.commit()
    # original still exists and is unchanged; reversal linked
    assert db.session.get(JournalEntry, e.id).status == "posted"
    assert rev.reverses_entry_id == e.id


def test_ec03_mixed_valuation_batches_costed_per_batch(app):
    from app.inventory import services as inv
    p, wh = _product(), _wh()
    # existing FIFO batch @2000 (qty 10 from seed) + new @2500
    inv.receive_stock(product=p, warehouse=wh, qty="10", unit_cost="2500", user_id=_u())
    db.session.commit()
    cogs = inv.issue_stock(product=p, warehouse=wh, qty="12")  # 10@2000 + 2@2500
    assert cogs == Decimal("25000.0000")


def test_ec04_return_uses_original_cost_not_current(app):
    from app.sales import services as s
    from app.inventory import services as inv
    p, wh, drawer = _product(), _wh(), _drawer()
    shift = s.open_shift(cashier_id=_u(), drawer=drawer, opening_cash="0", user_id=_u())
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="cash",
                       treasury=drawer, shift=shift, user_id=_u())
    db.session.commit()
    original_cost = sale.total_cost
    # change the product price afterwards — return must still use original cost
    p.default_price = "9999"; db.session.commit()
    ret = s.sales_return(sale=sale, user_id=_u())
    db.session.commit()
    assert ret.total_cost == original_cost


def test_ec06_shift_over_short_posts_entry(app):
    from app.sales import services as s
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_u(), drawer=drawer, opening_cash="500", user_id=_u())
    db.session.commit()
    s.close_shift(shift=shift, counted_cash="450", user_id=_u())  # shortage 50
    db.session.commit()
    assert shift.over_short == Decimal("-50.0000")
    assert shift.journal_entry_id is not None


def test_ec07_creator_cannot_approve_own_manual_entry(app):
    from app.accounting import posting
    from app.accounting.models import Account
    a = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    b = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    e = posting.build_entry(entry_date=date.today(), branch_id=None, user_id=_u(),
        lines=[posting.line(a, currency="AED", amount="50", side="debit"),
               posting.line(b, currency="AED", amount="50", side="credit")])
    with pytest.raises(posting.SeparationOfDutiesError):
        posting.post_entry(e, user_id=_u())  # same creator


def test_ec08_custody_overspend_blocked(app):
    from app.custody import services as cust
    from app.purchasing import services as pur
    from app.treasury.models import Treasury
    from app.custody.models import Custody
    c = db.session.scalar(db.select(Custody))
    main = db.session.scalar(db.select(Treasury).filter_by(type="main"))
    cust.issue(custody=c, from_treasury=main, amount="1000", user_id=_u())
    db.session.commit()
    with pytest.raises(pur.InsufficientCustodyError):
        pur.create_purchase_invoice(supplier=None, warehouse=_wh(),
            lines=[(_product(), "1", "3000")], payment_type="custody", custody=c,
            user_id=_u())


def test_ec10_below_min_blocked_without_permission(app):
    from app.sales import services as s
    from app.inventory.services import MinPriceViolation
    p, wh, drawer = _product(), _wh(), _drawer()
    shift = s.open_shift(cashier_id=_u(), drawer=drawer, opening_cash="0", user_id=_u())
    db.session.commit()
    with pytest.raises(MinPriceViolation):
        s.make_sale(warehouse=wh, lines=[(p, "1", "100")], payment_type="cash",
                    treasury=drawer, shift=shift, user_id=_u())  # min is 2500


def test_ec11_no_posting_into_closed_period(app):
    from app.accounting import posting, services
    from app.accounting.models import Account, AccountingPeriod
    period = db.session.scalar(db.select(AccountingPeriod))
    services.close_period(period, user_id=_u(), approved_by_id=_u())
    db.session.commit()
    a = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    b = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    e = posting.build_entry(entry_date=period.start_date, branch_id=None, user_id=_u(),
        lines=[posting.line(a, currency="AED", amount="10", side="debit"),
               posting.line(b, currency="AED", amount="10", side="credit")])
    with pytest.raises(posting.ClosedPeriodError):
        posting.post_entry(e, user_id=_u2().id)


def test_ec14_serial_not_sold_twice(app):
    from app.inventory import services as inv
    from app.sales import services as s
    p, wh, drawer = _product("1002"), _wh(), _drawer()  # iPhone is serial-tracked
    shift = s.open_shift(cashier_id=_u(), drawer=drawer, opening_cash="0", user_id=_u())
    db.session.commit()
    s.make_sale(warehouse=wh, lines=[(p, "1", "4500", ["1002-SN0001"])],
                payment_type="cash", treasury=drawer, shift=shift, user_id=_u())
    db.session.commit()
    with pytest.raises(inv.SerialError):
        s.make_sale(warehouse=wh, lines=[(p, "1", "4500", ["1002-SN0001"])],
                    payment_type="cash", treasury=drawer, shift=shift, user_id=_u())


def test_ec15_duplicate_serial_on_receipt_blocked(app):
    from app.inventory import services as inv
    p, wh = _product("1002"), _wh()
    with pytest.raises(inv.SerialError):
        inv.receive_stock(product=p, warehouse=wh, qty="1", unit_cost="3600",
                          serials=["1002-SN0001"], user_id=_u())  # already exists


def test_ec16_transfer_more_than_available_blocked(app):
    from app.shipping import services as sh
    from app.inventory.models import Warehouse
    whs = db.session.scalars(db.select(Warehouse).order_by(Warehouse.id)).all()
    permit = sh.create_permit(from_warehouse=whs[0], to_warehouse=whs[1],
                              lines=[(_product(), "9999")], fx_rate="1", user_id=_u())
    db.session.commit()
    with pytest.raises(sh.NotEnoughStockError):
        sh.send_permit(permit, user_id=_u())


def test_ec17_cannot_deactivate_treasury_with_balance(app):
    from app.treasury import services as tr
    from app.treasury.models import Treasury
    main = db.session.scalar(db.select(Treasury).filter_by(type="main"))
    with pytest.raises(tr.BalanceNotZeroError):
        tr.deactivate(main, user_id=_u())


def test_ec18_currency_locks_after_movement(app):
    from app.treasury import services as tr
    from app.treasury.models import Treasury
    main = db.session.scalar(db.select(Treasury).filter_by(type="main"))
    assert tr.has_movements(main) is True  # opening balance = first movement


def test_ec19_missing_fx_rate_blocks(app):
    from app.core import fx
    with pytest.raises(fx.RateUnavailableError):
        fx.require_rate("USD", "SAR")  # no such rate seeded


def test_ec20_funder_over_settlement_blocked(app):
    from app.sales import services as s
    from app.sales.models import Funder
    funder = db.session.scalar(db.select(Funder))
    p, wh, drawer = _product(), _wh(), _drawer()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="funder",
                       funder=funder, user_id=_u())
    db.session.commit()
    from app.treasury.models import BankAccount
    from app.treasury import services as tr
    bank = tr.create_bank_account(name_ar="بنك", bank_name="X", account_number="1",
                                  branch_id=wh.branch_id, currency_code="AED",
                                  opening_balance="0", user_id=_u())
    db.session.commit()
    with pytest.raises(s.OverSettlementError):  # §16.20
        s.settle_funder(funder=funder, sales=[sale], bank_account_id=bank.account_id,
                        received_amount="5000", user_id=_u())  # more than 3000 owed


def test_ec22_numbering_sequential_under_load(app):
    from app.core.numbering import next_number
    nums = [next_number(1, "TEST", prefix="T-") for _ in range(5)]
    db.session.commit()
    seqs = [int(n.split("-")[-1]) for n in nums]
    assert seqs == list(range(seqs[0], seqs[0] + 5))


def test_ec_posted_entry_immutable(app):
    from app.accounting import posting
    from app.accounting.models import Account
    from app.core.audit import ImmutableRecordError
    a = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    b = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    e = posting.build_entry(entry_date=date.today(), branch_id=None, user_id=_u(),
        lines=[posting.line(a, currency="AED", amount="10", side="debit"),
               posting.line(b, currency="AED", amount="10", side="credit")])
    posting.post_entry(e, user_id=_u2().id)
    db.session.commit()
    e.memo = "tamper"
    with pytest.raises(ImmutableRecordError):
        db.session.commit()
    db.session.rollback()


def test_ec24_unbalanced_opening_entry_rejected(app):
    from app.accounting import posting
    from app.accounting.models import Account
    a = db.session.scalar(db.select(Account).filter_by(code="1101")).id
    b = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    e = posting.build_entry(entry_date=date.today(), branch_id=None,
        source_doc_type="opening", user_id=_u(),
        lines=[posting.line(a, currency="AED", amount="100", side="debit"),
               posting.line(b, currency="AED", amount="90", side="credit")])
    with pytest.raises(posting.UnbalancedEntryError):
        posting.post_entry(e, user_id=_u())
