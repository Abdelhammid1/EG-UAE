"""Phase 4 — purchasing, suppliers & consignments (spec §9, §12)."""
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

    db.session.add(Currency(code="AED", name_ar="درهم", name_en="AED", decimal_places=2))
    chart = {"11": ("نقدية", "asset", False), "1203": ("عهد", "asset", False),
             "1301": ("مخزون", "asset", True), "2101": ("موردون", "liability", False),
             "3103": ("افتتاحي", "equity", True), "5104": ("خسارة فرق", "expense", True),
             "4102": ("ربح فرق", "revenue", True), "5201": ("مصروف نقل", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("fx.loss", "5104"), ("fx.gain", "4102")]:
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
    db.session.add(Product(name_ar="هاتف", default_price="3000", min_price="2000",
                           valuation_method="FIFO"))
    db.session.commit()


def _ctx():
    from app.inventory.models import Product, Warehouse
    from app.auth.models import User
    from app.core.models import Branch
    return (db.session.scalar(db.select(Product)),
            db.session.scalar(db.select(Warehouse)),
            db.session.scalar(db.select(User)).id,
            db.session.scalar(db.select(Branch)).id)


# --- supplier + credit purchase -------------------------------------------
def test_credit_purchase_increases_stock_and_ap(app):
    from app.purchasing import services as pur
    from app.inventory import services as inv
    p, wh, uid, bid = _ctx()
    supplier = pur.create_supplier(name_ar="مورد", currency_code="AED", branch_id=bid,
                                   user_id=uid)
    db.session.commit()
    inv_ = pur.create_purchase_invoice(supplier=supplier, warehouse=wh,
                                       lines=[(p, "10", "2000")],
                                       payment_type="credit", user_id=uid)
    db.session.commit()
    assert inv.qty_on_hand(p.id, wh.id) == Decimal("10.0000")
    assert pur.ap_balance(supplier) == Decimal("20000.0000")  # we owe 20000
    assert inv_.journal_entry.is_balanced


def test_cash_purchase_pays_from_treasury(app):
    from app.purchasing import services as pur
    from app.treasury import services as tr
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    supplier = pur.create_supplier(name_ar="مورد", branch_id=bid, user_id=uid)
    db.session.commit()
    pur.create_purchase_invoice(supplier=supplier, warehouse=wh,
                                lines=[(p, "5", "2000")], payment_type="cash",
                                pay_from_account_id=t.account_id, user_id=uid)
    db.session.commit()
    assert tr.balance_native(t.account_id) == Decimal("40000.0000")  # 50000-10000


def test_supplier_payment_clears_ap(app):
    from app.purchasing import services as pur
    from app.treasury import services as tr
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    supplier = pur.create_supplier(name_ar="مورد", branch_id=bid, user_id=uid)
    db.session.commit()
    inv_ = pur.create_purchase_invoice(supplier=supplier, warehouse=wh,
                                       lines=[(p, "10", "2000")],
                                       payment_type="credit", user_id=uid)
    db.session.commit()
    pur.create_supplier_payment(supplier=supplier, pay_from_account_id=t.account_id,
                                amount="12000", allocations=[(inv_, "12000")],
                                branch_id=bid, user_id=uid)
    db.session.commit()
    assert pur.ap_balance(supplier) == Decimal("8000.0000")  # 20000 - 12000
    assert pur.invoice_outstanding(inv_) == Decimal("8000.0000")


# --- consignments (spec §12) ----------------------------------------------
def test_custody_issue_and_available(app):
    from app.custody import services as cust
    from app.treasury import services as tr
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    c = cust.create_custody(name_ar="مندوب", branch_id=bid, currency_code="AED",
                            user_id=uid)
    db.session.commit()
    cust.issue(custody=c, from_treasury=t, amount="5000", user_id=uid)
    db.session.commit()
    assert cust.available(c) == Decimal("5000.0000")
    assert tr.balance_native(t.account_id) == Decimal("45000.0000")


def test_custody_purchase_deducts_available(app):
    """The §12 worked example: 5000 issued, 3200 purchase -> 1800 available."""
    from app.custody import services as cust
    from app.purchasing import services as pur
    from app.treasury import services as tr
    from app.inventory import services as inv
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    c = cust.create_custody(name_ar="مندوب", branch_id=bid, currency_code="AED",
                            user_id=uid)
    db.session.commit()
    cust.issue(custody=c, from_treasury=t, amount="5000", user_id=uid)
    db.session.commit()
    pur.create_purchase_invoice(supplier=None, warehouse=wh,
                                lines=[(p, "1", "3200")], payment_type="custody",
                                custody=c, user_id=uid)
    db.session.commit()
    assert cust.available(c) == Decimal("1800.0000")  # 5000 - 3200
    assert inv.qty_on_hand(p.id, wh.id) == Decimal("1.0000")


def test_custody_overspend_blocked(app):
    from app.custody import services as cust
    from app.purchasing import services as pur
    from app.treasury import services as tr
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    c = cust.create_custody(name_ar="مندوب", branch_id=bid, currency_code="AED",
                            user_id=uid)
    db.session.commit()
    cust.issue(custody=c, from_treasury=t, amount="2000", user_id=uid)
    db.session.commit()
    with pytest.raises(pur.InsufficientCustodyError):  # §12.3
        pur.create_purchase_invoice(supplier=None, warehouse=wh,
                                    lines=[(p, "1", "3000")],
                                    payment_type="custody", custody=c, user_id=uid)


def test_custody_close_returns_balance(app):
    from app.custody import services as cust
    from app.treasury import services as tr
    p, wh, uid, bid = _ctx()
    t = tr.create_treasury(name_ar="خزينة", branch_id=bid, currency_code="AED",
                           opening_balance="50000", user_id=uid)
    c = cust.create_custody(name_ar="مندوب", branch_id=bid, currency_code="AED",
                            user_id=uid)
    db.session.commit()
    cust.issue(custody=c, from_treasury=t, amount="5000", user_id=uid)
    db.session.commit()
    cust.close(custody=c, to_treasury=t, user_id=uid)
    db.session.commit()
    assert c.status == "closed"
    assert cust.available(c) == Decimal("0.0000")
    assert tr.balance_native(t.account_id) == Decimal("50000.0000")  # all returned
