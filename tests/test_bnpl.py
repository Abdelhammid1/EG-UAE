"""Phase 6b — BNPL funders + internal installments (spec §10)."""
from datetime import date, timedelta
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
    chart = {"11": ("نقدية", "asset", False), "1102": ("بنك", "asset", True),
             "1201": ("عملاء", "asset", False), "1202": ("جهات تمويل", "asset", False),
             "1301": ("مخزون", "asset", True), "3103": ("افتتاحي", "equity", True),
             "4101": ("مبيعات", "revenue", True), "5101": ("cogs", "expense", True),
             "5102": ("عمولة تمويل", "expense", True)}
    obj = {}
    for code, (n, t, p) in chart.items():
        a = Account(code=code, name_ar=n, type=t, is_postable=p)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("inventory", "1301"), ("opening.equity", "3103"),
                     ("sales.revenue", "4101"), ("cogs", "5101"),
                     ("funder.commission", "5102"), ("cash.main", "11")]:
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
    p = Product(name_ar="هاتف", default_price="3000", min_price="1000",
                valuation_method="FIFO")
    db.session.add(p); db.session.flush()
    inv.receive_stock(product=p, warehouse=wh, qty="50", unit_cost="2000", user_id=u.id)
    db.session.commit()


def _ctx():
    from app.inventory.models import Product, Warehouse
    from app.auth.models import User
    from app.core.models import Branch
    return (db.session.scalar(db.select(Product)),
            db.session.scalar(db.select(Warehouse)),
            db.session.scalar(db.select(User)).id,
            db.session.scalar(db.select(Branch)).id)


# --- BNPL funder ----------------------------------------------------------
def test_funder_sale_and_settlement(app):
    """Sale via funder → receivable on funder; settlement books commission."""
    from app.sales import services as s
    from app.accounting.models import Account
    p, wh, uid, bid = _ctx()
    funder = s.create_funder(name_ar="تابي", branch_id=bid, currency_code="AED",
                             commission_pct="6", user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="funder",
                       funder=funder, user_id=uid)
    db.session.commit()
    # receivable sits on the funder, not a customer
    assert s.funder_open_receivable(funder) == Decimal("3000.0000")
    assert sale.funder_id == funder.id
    # funder pays 2820 (6% commission = 180)
    bank = db.session.scalar(db.select(Account).filter_by(code="1102")).id
    st = s.settle_funder(funder=funder, sales=[sale], bank_account_id=bank,
                         received_amount="2820", user_id=uid)
    db.session.commit()
    assert st.commission == Decimal("180.0000")
    assert s.funder_open_receivable(funder) == Decimal("0.0000")  # cleared
    assert sale.funder_settled is True


def test_funder_settle_multiple(app):
    from app.sales import services as s
    from app.accounting.models import Account
    from app.accounting.services import trial_balance
    p, wh, uid, bid = _ctx()
    funder = s.create_funder(name_ar="تمارا", branch_id=bid, commission_pct="5",
                             user_id=uid)
    db.session.commit()
    s1 = s.make_sale(warehouse=wh, lines=[(p, "1", "2000")], payment_type="funder", funder=funder, user_id=uid)
    s2 = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="funder", funder=funder, user_id=uid)
    db.session.commit()
    assert s.funder_open_receivable(funder) == Decimal("5000.0000")
    bank = db.session.scalar(db.select(Account).filter_by(code="1102")).id
    s.settle_funder(funder=funder, sales=[s1, s2], bank_account_id=bank,
                    received_amount="4750", user_id=uid)  # 250 commission
    db.session.commit()
    assert s.funder_open_receivable(funder) == Decimal("0.0000")
    assert trial_balance()["balanced"]


# --- installments ---------------------------------------------------------
def test_installment_schedule_generated(app):
    from app.sales import services as s
    p, wh, uid, bid = _ctx()
    cust = s.create_customer(name_ar="عميل", credit_limit="0", branch_id=bid, user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")],
                       payment_type="installment", customer=cust,
                       installment={"count": 3, "first_due": date(2026, 2, 1),
                                    "down_payment": 0}, user_id=uid)
    db.session.commit()
    from app.sales.models import InstallmentPlan
    plan = db.session.scalar(db.select(InstallmentPlan).filter_by(sale_id=sale.id))
    assert plan.count == 3
    assert len(plan.dues) == 3
    assert sum(d.amount for d in plan.dues) == Decimal("3000.0000")  # 1000 each
    assert s.ar_balance(cust) == Decimal("3000.0000")


def test_collection_pays_oldest_installment(app):
    from app.sales import services as s
    from app.accounting.models import Account
    p, wh, uid, bid = _ctx()
    cust = s.create_customer(name_ar="عميل", credit_limit="0", branch_id=bid, user_id=uid)
    db.session.commit()
    sale = s.make_sale(warehouse=wh, lines=[(p, "1", "3000")],
                       payment_type="installment", customer=cust,
                       installment={"count": 3, "first_due": date(2026, 2, 1)},
                       user_id=uid)
    db.session.commit()
    cash = db.session.scalar(db.select(Account).filter_by(code="11")).id
    s.collect(customer=cust, to_account_id=cash, amount="1000", branch_id=bid, user_id=uid)
    db.session.commit()
    from app.sales.models import InstallmentPlan
    plan = db.session.scalar(db.select(InstallmentPlan).filter_by(sale_id=sale.id))
    assert plan.dues[0].is_paid is True
    assert plan.dues[1].is_paid is False
    assert s.ar_balance(cust) == Decimal("2000.0000")


def test_overdue_installments_report(app):
    from app.sales import services as s
    p, wh, uid, bid = _ctx()
    cust = s.create_customer(name_ar="عميل", credit_limit="0", branch_id=bid, user_id=uid)
    db.session.commit()
    yesterday = date.today() - timedelta(days=1)
    s.make_sale(warehouse=wh, lines=[(p, "1", "3000")], payment_type="installment",
                customer=cust, installment={"count": 2, "first_due": yesterday},
                user_id=uid)
    db.session.commit()
    overdue = s.overdue_installments()
    assert len(overdue) >= 1
