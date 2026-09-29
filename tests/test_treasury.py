"""Phase 2 — cash & banks (spec §6)."""
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
    db.session.add_all([
        Currency(code="AED", name_ar="درهم", name_en="AED", decimal_places=2),
        Currency(code="EGP", name_ar="جنيه", name_en="EGP", decimal_places=2),
    ])
    # chart: cash parent + equity + fx + capital
    accs = {
        "11": ("النقدية والبنوك", "asset", False),
        "3103": ("رصيد افتتاحي", "equity", True),
        "3101": ("رأس المال", "equity", True),
        "4102": ("أرباح فروق", "revenue", True),
        "5104": ("خسائر فروق", "expense", True),
        "5103": ("مصروفات بنكية", "expense", True),
    }
    obj = {}
    for code, (name, t, postable) in accs.items():
        a = Account(code=code, name_ar=name, type=t, is_postable=postable)
        db.session.add(a); db.session.flush(); obj[code] = a
    for op, code in [("opening.equity", "3103"), ("fx.gain", "4102"),
                     ("fx.loss", "5104"), ("bank.fee", "5103")]:
        db.session.add(AccountMapping(operation_code=op, branch_id=None,
                                      account_id=obj[code].id))
    c = Country(name_ar="مصر", name_en="EG", iso_code="EG", currency_code="EGP")
    db.session.add(c); db.session.flush()
    db.session.add(Branch(name_ar="فرع", name_en="Branch", country_id=c.id))
    db.session.add(AccountingPeriod(name="2026", start_date=date(2026, 1, 1),
                                    end_date=date(2026, 12, 31), status="open"))
    u = User(username="u1", full_name="u1"); u.set_password("x")
    u2 = User(username="u2", full_name="u2"); u2.set_password("x")
    db.session.add_all([u, u2])
    db.session.commit()


def _branch():
    from app.core.models import Branch
    return db.session.scalar(db.select(Branch)).id


def _uid(n="u1"):
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username=n)).id


# --- opening balance ------------------------------------------------------
def test_treasury_opening_balance_posts_entry(app):
    from app.treasury import services
    t = services.create_treasury(name_ar="خزينة", branch_id=_branch(),
                                  currency_code="AED", opening_balance="1000",
                                  user_id=_uid())
    db.session.commit()
    assert services.balance_native(t.account_id) == Decimal("1000.0000")


# --- deposit / withdraw ---------------------------------------------------
def test_deposit_and_withdraw(app):
    from app.treasury import services
    from app.accounting.models import Account
    t = services.create_treasury(name_ar="خزينة", branch_id=_branch(),
                                  currency_code="AED", user_id=_uid())
    cap = db.session.scalar(db.select(Account).filter_by(code="3101")).id
    services.deposit(entity=t, counter_account_id=cap, amount="500", user_id=_uid())
    services.withdraw(entity=t, counter_account_id=cap, amount="200", user_id=_uid())
    db.session.commit()
    assert services.balance_native(t.account_id) == Decimal("300.0000")


# --- internal transfer ----------------------------------------------------
def test_internal_transfer_same_currency(app):
    from app.treasury import services
    a = services.create_treasury(name_ar="A", branch_id=_branch(),
                                 currency_code="AED", opening_balance="1000", user_id=_uid())
    b = services.create_treasury(name_ar="B", branch_id=_branch(),
                                 currency_code="AED", user_id=_uid())
    db.session.commit()
    services.internal_transfer(src=a, dst=b, amount="400", user_id=_uid())
    db.session.commit()
    assert services.balance_native(a.account_id) == Decimal("600.0000")
    assert services.balance_native(b.account_id) == Decimal("400.0000")


def test_internal_transfer_rejects_currency_mismatch(app):
    from app.treasury import services
    a = services.create_treasury(name_ar="A", branch_id=_branch(), currency_code="AED", user_id=_uid())
    b = services.create_treasury(name_ar="B", branch_id=_branch(), currency_code="EGP", user_id=_uid())
    db.session.commit()
    with pytest.raises(ValueError):
        services.internal_transfer(src=a, dst=b, amount="100", user_id=_uid())


# --- cross-currency transfer (spec §6 UAE -> Egypt) -----------------------
def test_fx_transfer_books_difference(app):
    from app.treasury import services
    from app.accounting.services import trial_balance
    # UAE dirham account -> Egypt pound account
    uae = services.create_treasury(name_ar="الإمارات", branch_id=_branch(),
                                   currency_code="AED", opening_balance="10000", user_id=_uid())
    egy = services.create_treasury(name_ar="مصر", branch_id=_branch(),
                                   currency_code="EGP", user_id=_uid())
    db.session.commit()
    # send 1000 AED (rate 1 to book AED), receive 12000 EGP at 0.083 -> 996 book
    services.fx_transfer(src=uae, dst=egy, sent_amount="1000", received_amount="12000",
                         src_rate="1", dst_rate="0.083", user_id=_uid())
    db.session.commit()
    assert services.balance_native(uae.account_id) == Decimal("9000.0000")  # 10000-1000
    assert services.balance_native(egy.account_id) == Decimal("12000.0000")  # native EGP
    # whole ledger still balances (difference booked to FX)
    assert trial_balance()["balanced"]


# --- currency lock (spec §16.18) ------------------------------------------
def test_has_movements_after_opening(app):
    from app.treasury import services
    t = services.create_treasury(name_ar="خزينة", branch_id=_branch(),
                                 currency_code="AED", opening_balance="100", user_id=_uid())
    db.session.commit()
    assert services.has_movements(t) is True


# --- deactivate rules (spec §16.17) ---------------------------------------
def test_cannot_deactivate_with_balance(app):
    from app.treasury import services
    t = services.create_treasury(name_ar="خزينة", branch_id=_branch(),
                                 currency_code="AED", opening_balance="100", user_id=_uid())
    db.session.commit()
    with pytest.raises(services.BalanceNotZeroError):
        services.deactivate(t, user_id=_uid())


# --- approval threshold (spec §6) -----------------------------------------
def test_transfer_over_limit_needs_approval(app):
    from app.core import settings as st
    from app.admin.models import SettingDefinition
    from app.treasury import services
    from app.auth.models import User
    # define + set a low approval limit
    db.session.add(SettingDefinition(
        key="treasury.transfer_approval_limit", section="treasury",
        value_type="decimal", scope="global", label_ar="حد", label_en="limit",
        default_value=None))
    db.session.flush()
    st.set("treasury.transfer_approval_limit", "100", user_id=_uid())
    a = services.create_treasury(name_ar="A", branch_id=_branch(), currency_code="AED",
                                 opening_balance="1000", user_id=_uid())
    b = services.create_treasury(name_ar="B", branch_id=_branch(), currency_code="AED",
                                 user_id=_uid())
    db.session.commit()
    non_owner = db.session.scalar(db.select(User).filter_by(username="u1"))
    txn = services.internal_transfer(src=a, dst=b, amount="500", user_id=_uid(),
                                     user=non_owner)
    db.session.commit()
    assert txn.status == "pending_approval"
    # balance unchanged until approved
    assert services.balance_native(a.account_id) == Decimal("1000.0000")
    services.approve_and_post(txn, user_id=_uid())
    db.session.commit()
    assert txn.status == "posted"
    assert services.balance_native(a.account_id) == Decimal("500.0000")
