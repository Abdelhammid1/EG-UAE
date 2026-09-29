"""Remaining features: POs, quotes, split payment, partial returns, wallet,
first-login password, bank-rec import."""
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


# --- purchase orders ------------------------------------------------------
def test_purchase_order_converts_to_invoice(app):
    from app.purchasing import services as pur
    from app.purchasing.models import Supplier
    from app.inventory import services as inv
    supplier = pur.create_supplier(name_ar="مورد", branch_id=1, user_id=_uid())
    db.session.commit()
    order = pur.create_order(supplier=supplier, warehouse=_wh(),
                             lines=[(_prod(), "5", "2000")], user_id=_uid())
    db.session.commit()
    assert order.status == "draft"
    assert order.total == Decimal("10000.0000")
    pur.approve_order(order, user_id=_uid())
    invoice = pur.convert_order_to_invoice(order, payment_type="credit", user_id=_uid())
    db.session.commit()
    assert order.status == "received"
    assert order.invoice_id == invoice.id
    assert inv.qty_on_hand(_prod().id, _wh().id) >= Decimal("5.0000")


# --- quotes ---------------------------------------------------------------
def test_quote_converts_to_sale_rechecking_min(app):
    from app.sales import services as s
    from app.sales.models import Customer
    from app.inventory.services import MinPriceViolation
    cust = s.create_customer(name_ar="عميل", credit_limit="0", branch_id=1, user_id=_uid())
    db.session.commit()
    # a quote below the product min (2500) — conversion must re-check and block
    q = s.create_quote(customer=cust, warehouse=_wh(),
                       lines=[(_prod(), "1", "1000")], user_id=_uid())
    db.session.commit()
    with pytest.raises(MinPriceViolation):
        s.convert_quote_to_sale(q, payment_type="credit", user_id=_uid())


def test_quote_valid_converts(app):
    from app.sales import services as s
    from app.sales.models import Customer
    cust = s.create_customer(name_ar="عميل", credit_limit="0", branch_id=1, user_id=_uid())
    db.session.commit()
    q = s.create_quote(customer=cust, warehouse=_wh(),
                       lines=[(_prod(), "1", "3000")], user_id=_uid())
    db.session.commit()
    sale = s.convert_quote_to_sale(q, payment_type="credit", user_id=_uid())
    db.session.commit()
    assert q.sale_id == sale.id
    assert s.ar_balance(cust) == Decimal("3000.0000")


# --- split payment --------------------------------------------------------
def test_split_payment(app):
    from app.sales import services as s
    from app.treasury import services as tr
    from app.accounting.services import trial_balance
    drawer = _drawer()
    bank = tr.create_bank_account(name_ar="بنك", bank_name="X", account_number="1",
                                  branch_id=2, currency_code="AED", opening_balance="0",
                                  user_id=_uid())
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    # sell 6000: 2000 cash + 4000 card
    sale = s.make_sale(warehouse=_wh(), lines=[(_prod(), "2", "3000")],
                       payment_type="split", treasury=drawer, bank_account=bank,
                       shift=shift,
                       payments=[("cash", "2000", drawer.account_id),
                                 ("card", "4000", bank.account_id)], user_id=_uid())
    db.session.commit()
    assert tr.balance_native(drawer.account_id) == Decimal("2000.0000")
    assert tr.balance_native(bank.account_id) == Decimal("4000.0000")
    assert trial_balance()["balanced"]


def test_split_payment_must_sum_to_total(app):
    from app.sales import services as s
    from app.treasury import services as tr
    drawer = _drawer()
    bank = tr.create_bank_account(name_ar="بنك", bank_name="X", account_number="1",
                                  branch_id=2, currency_code="AED", opening_balance="0",
                                  user_id=_uid())
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    with pytest.raises(ValueError):
        s.make_sale(warehouse=_wh(), lines=[(_prod(), "1", "3000")],
                    payment_type="split", treasury=drawer, bank_account=bank, shift=shift,
                    payments=[("cash", "1000", drawer.account_id)], user_id=_uid())


# --- partial line-level return --------------------------------------------
def test_partial_return(app):
    from app.sales import services as s
    from app.inventory import services as inv
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0", user_id=_uid())
    db.session.commit()
    sale = s.make_sale(warehouse=_wh(), lines=[(_prod(), "3", "3000")],
                       payment_type="cash", treasury=drawer, shift=shift, user_id=_uid())
    db.session.commit()
    line = sale.lines[0]
    start = inv.qty_on_hand(_prod().id, _wh().id)
    # return only 1 of the 3
    s.sales_return(sale=sale, line_qtys={line.id: "1"}, user_id=_uid())
    db.session.commit()
    assert inv.qty_on_hand(_prod().id, _wh().id) == start + Decimal("1.0000")
    assert sale.returned is False  # only partial
    assert s.returned_qty(line) == Decimal("1.0000")
    # cannot return more than remaining (2 left)
    with pytest.raises(s.AlreadyReturnedError):
        s.sales_return(sale=sale, line_qtys={line.id: "5"}, user_id=_uid())


# --- e-wallet type --------------------------------------------------------
def test_wallet_treasury_type(app):
    from app.treasury import services as tr
    from app.treasury.models import TREASURY_TYPES
    assert "wallet" in TREASURY_TYPES
    w = tr.create_treasury(name_ar="فودافون كاش", branch_id=1, currency_code="AED",
                           type="wallet", opening_balance="500", user_id=_uid())
    db.session.commit()
    assert w.type == "wallet"
    assert tr.balance_native(w.account_id) == Decimal("500.0000")


# --- first-login password change ------------------------------------------
def test_must_change_password_flag(app):
    from app.auth.models import User
    u = User(username="temp", full_name="مؤقت", must_change_password=True)
    u.set_password("temp1234")
    db.session.add(u); db.session.commit()
    assert u.must_change_password is True
    u.set_password("newpass12"); u.must_change_password = False
    db.session.commit()
    assert u.must_change_password is False


# --- bank reconciliation import -------------------------------------------
def test_bank_statement_auto_match(app):
    import io
    from openpyxl import Workbook
    from app.treasury import services as tr
    from app.accounting.models import Account
    bank = tr.create_bank_account(name_ar="بنك", bank_name="X", account_number="1",
                                  branch_id=2, currency_code="AED", opening_balance="0",
                                  user_id=_uid())
    cap = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    tr.deposit(entity=bank, counter_account_id=cap, amount="1500", user_id=_uid())
    tr.deposit(entity=bank, counter_account_id=cap, amount="2500", user_id=_uid())
    db.session.commit()
    # statement lists the two amounts
    wb = Workbook(); ws = wb.active
    ws.append(["date", "desc", "amount"])
    ws.append(["2026-01-01", "إيداع", 1500])
    ws.append(["2026-01-02", "إيداع", 2500])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    matched, unmatched = tr.import_statement(bank, buf, user_id=_uid())
    db.session.commit()
    assert matched == 2 and unmatched == 0
