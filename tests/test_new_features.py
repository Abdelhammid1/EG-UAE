"""Tests for the final feature wave: wholesale pricing, hold/recall, exchange,
per-expense custody approval, partial transfer receipt, configurable numbering,
approvals inbox, barcode/PDF, migration column-mapping, async reports."""
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


def _uid(username="owner"):
    from app.auth.models import User
    return db.session.scalar(db.select(User).filter_by(username=username)).id


def _wh():
    from app.inventory.models import Warehouse
    return db.session.scalar(db.select(Warehouse))


def _prod(bc="1001"):
    from app.inventory.models import Product
    return db.session.scalar(db.select(Product).filter_by(barcode=bc))


def _drawer():
    from app.treasury.models import Treasury
    return db.session.scalar(db.select(Treasury).filter_by(type="drawer"))


def _expense_account():
    from app.accounting.models import Account
    return db.session.scalar(
        db.select(Account).filter_by(type="expense", is_postable=True))


# --- SAL-02: wholesale vs retail pricing ----------------------------------
def test_price_for_wholesale_customer(app):
    from app.sales.models import Customer
    from app.accounting.models import Account
    p = _prod("1001")
    p.default_price = Decimal("100")
    p.wholesale_price = Decimal("80")
    acc = db.session.scalar(db.select(Account).filter_by(is_postable=True))
    retail = Customer(name_ar="قطاعي", type="retail", account_id=acc.id)
    whole = Customer(name_ar="جملة", type="wholesale", account_id=acc.id)
    assert p.price_for(retail) == Decimal("100")
    assert p.price_for(whole) == Decimal("80")
    assert p.price_for(None) == Decimal("100")
    # falls back to retail when no wholesale price is set
    p.wholesale_price = Decimal("0")
    assert p.price_for(whole) == Decimal("100")


# --- POS-02.7: hold / recall ---------------------------------------------
def test_hold_sale_roundtrip(app):
    import json
    from app.sales.models import HeldSale
    payload = json.dumps({"cart": [{"id": 1, "qty": 2, "price": 100}],
                          "warehouseId": _wh().id})
    h = HeldSale(label="فاتورة اختبار", branch_id=_wh().branch_id,
                 cashier_id=_uid(), payload=payload)
    db.session.add(h)
    db.session.commit()
    got = db.session.scalar(db.select(HeldSale))
    assert got.label == "فاتورة اختبار"
    assert json.loads(got.payload)["cart"][0]["qty"] == 2


# --- POS exchange (return + new sale, settle difference) ------------------
def test_make_exchange_difference_and_stock(app):
    from app.sales import services as s
    from app.inventory import services as inv
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0",
                         user_id=_uid())
    db.session.commit()
    p = _prod("1001")
    sale = s.make_sale(warehouse=_wh(), lines=[(p, "2", "3000")],
                       payment_type="cash", treasury=drawer, shift=shift,
                       user_id=_uid())
    db.session.commit()
    stock_after_sale = inv.qty_on_hand(p.id, _wh().id)
    # exchange: return 1 unit of p, buy 1 unit of a different product
    p2 = _prod("1003")
    line = sale.lines[0]
    result = s.make_exchange(
        original_sale=sale, return_line_qtys={line.id: "1"},
        new_lines=[(p2, "1", "5000")], warehouse=_wh(), shift=shift,
        treasury=drawer, payment_type="cash", user_id=_uid())
    db.session.commit()
    # returned 1 unit of p -> stock of p goes back up by 1
    assert inv.qty_on_hand(p.id, _wh().id) == stock_after_sale + Decimal("1.0000")
    # difference = new sale total - return total (5000 - 3000 = 2000, no tax)
    assert result["difference"] == Decimal("2000.0000")
    assert result["sale"].number != sale.number


def test_exchange_requires_new_lines(app):
    from app.sales import services as s
    drawer = _drawer()
    shift = s.open_shift(cashier_id=_uid(), drawer=drawer, opening_cash="0",
                         user_id=_uid())
    db.session.commit()
    p = _prod("1001")
    sale = s.make_sale(warehouse=_wh(), lines=[(p, "1", "3000")],
                       payment_type="cash", treasury=drawer, shift=shift,
                       user_id=_uid())
    db.session.commit()
    with pytest.raises((ValueError, Exception)):
        s.make_exchange(original_sale=sale, return_line_qtys={sale.lines[0].id: "1"},
                        new_lines=[], warehouse=_wh(), shift=shift, treasury=drawer,
                        user_id=_uid())


# --- CUS-04: per-expense custody approval ---------------------------------
def _open_custody(amount="5000"):
    from app.custody import services as cust
    from app.treasury.models import Treasury
    treasury = db.session.scalar(db.select(Treasury).filter_by(type="main")) \
        or db.session.scalar(db.select(Treasury))
    c = cust.create_custody(name_ar="مندوب", branch_id=treasury.branch_id,
                            currency_code=treasury.currency_code, user_id=_uid())
    cust.issue(custody=c, from_treasury=treasury, amount=amount, user_id=_uid())
    db.session.commit()
    return c


def test_custody_expense_below_limit_posts_immediately(app):
    from app.core import settings
    from app.custody import services as cust
    settings.set("custody.expense_approval_limit", "1000", user_id=_uid())
    c = _open_custody()
    txn = cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                             amount="500", memo="صغير", user_id=_uid())
    db.session.commit()
    assert txn.status == "posted"
    assert txn.journal_entry_id is not None


def test_custody_expense_above_limit_pends_then_approves(app):
    from app.core import settings
    from app.custody import services as cust
    settings.set("custody.expense_approval_limit", "1000", user_id=_uid())
    c = _open_custody()
    before = cust.available(c)
    txn = cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                             amount="2000", memo="كبير", user_id=_uid("admin"))
    db.session.commit()
    assert txn.status == "pending"
    assert txn.journal_entry_id is None
    # reserved: available_to_spend drops but posted balance unchanged
    assert cust.available(c) == before
    assert cust.available_to_spend(c) == before - Decimal("2000.0000")
    # owner approves (different user than creator)
    cust.approve_expense(txn, user_id=_uid())
    db.session.commit()
    assert txn.status == "posted"
    assert txn.journal_entry_id is not None
    assert cust.available(c) == before - Decimal("2000.0000")


def test_custody_expense_approval_separation_of_duties(app):
    from app.core import settings
    from app.custody import services as cust
    settings.set("custody.expense_approval_limit", "1000", user_id=_uid())
    c = _open_custody()
    txn = cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                             amount="2000", user_id=_uid("admin"))
    db.session.commit()
    with pytest.raises(cust.ApprovalError):
        cust.approve_expense(txn, user_id=_uid("admin"))  # same person


def test_custody_pending_reserves_against_overspend(app):
    from app.core import settings
    from app.custody import services as cust
    settings.set("custody.expense_approval_limit", "1000", user_id=_uid())
    c = _open_custody("3000")
    cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                       amount="2000", user_id=_uid("admin"))
    db.session.commit()
    # only 1000 left to spend; a 1500 expense must be blocked
    with pytest.raises(cust.OverspendError):
        cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                           amount="1500", user_id=_uid("admin"))


# --- TRF-05.6: partial transfer receipt -----------------------------------
def test_partial_transfer_receipt(app):
    from app.shipping import services as sh
    from app.inventory import services as inv
    from app.inventory.models import Warehouse
    from app.accounting.models import Account
    whs = db.session.scalars(db.select(Warehouse).order_by(Warehouse.id)).all()
    src, dst = whs[0], whs[1]
    p = _prod("1001")
    permit = sh.create_permit(from_warehouse=src, to_warehouse=dst,
                              lines=[(p, "3")], fx_rate="1", user_id=_uid())
    db.session.commit()
    sh.send_permit(permit, user_id=_uid())
    db.session.commit()
    line = permit.lines[0]
    # receive 2 of 3, keep the rest in transit
    sh.receive_permit(permit, received={line.id: "2"}, partial=True, user_id=_uid())
    db.session.commit()
    assert permit.status == "sent"
    assert permit.lines[0].qty_received == Decimal("2.0000")
    assert inv.qty_on_hand(p.id, dst.id) == Decimal("2.0000")
    # receive the remaining 1 as final -> received, transit cleared
    sh.receive_permit(permit, partial=False, user_id=_uid())
    db.session.commit()
    assert permit.status == "received"
    assert permit.lines[0].qty_received == Decimal("3.0000")
    from app.treasury.services import balance_native
    git = db.session.scalar(db.select(Account).filter_by(code="1302"))
    assert balance_native(git.id) == Decimal("0.0000")


# --- configurable numbering -----------------------------------------------
def test_configurable_numbering_format(app):
    from app.core import settings
    from app.core.numbering import next_number
    settings.set("numbering.sale", "S{yyyy}/{seq}", user_id=_uid())
    settings.set("numbering.seq_width", "4", user_id=_uid())
    from datetime import date
    n = next_number(1, "SALE", prefix="INV-")
    assert n == f"S{date.today().year}/0001"
    n2 = next_number(1, "SALE", prefix="INV-")
    assert n2.endswith("/0002")


def test_numbering_falls_back_on_bad_template(app):
    from app.core import settings
    from app.core.numbering import next_number
    settings.set("numbering.sale", "{nope}-{seq}", user_id=_uid())
    n = next_number(2, "SALE", prefix="INV-")
    assert n == "INV-02-000001"  # classic fallback


# --- approvals inbox ------------------------------------------------------
def test_approvals_summary_counts_pending(app):
    from app.core import settings
    from app.custody import services as cust
    from app.approvals import services as ap
    settings.set("custody.expense_approval_limit", "1000", user_id=_uid())
    c = _open_custody()
    cust.spend_expense(custody=c, expense_account_id=_expense_account().id,
                       amount="2000", user_id=_uid("admin"))
    db.session.commit()
    s = ap.summary()
    assert len(s["custody"]) == 1
    assert s["count"] >= 1
    assert ap.pending_count() >= 1


# --- barcode + PDF --------------------------------------------------------
def test_barcode_svg(app):
    from app.inventory.services import barcode_svg
    svg = barcode_svg("6221234567890")
    assert svg and "<svg" in svg
    assert barcode_svg("") is None


def test_pdf_build(app):
    from app.core.pdf import build_pdf
    pdf = build_pdf([
        {"type": "title", "text": "تقرير"},
        {"type": "table", "headers": ["أ", "ب"], "rows": [["س", "1,000.00"]],
         "aligns": ["R", "C"], "totals": ["المجموع", "1,000.00"]},
    ], title="t")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 500


# --- migration column mapping ---------------------------------------------
def test_suggest_and_parse_mapping(app):
    from app.migration import services as mig
    # headers in a different order than the template
    headers = ["الباركود", "اسم المنتج", "سعر البيع"]
    raw = [["1001", "هاتف", "3000"], ["1002", "شاحن", "100"]]
    mapping = mig.suggest_mapping("products", headers)
    # name should map to index 1, barcode to index 0
    assert mapping["name_ar"] == 1
    assert mapping["barcode"] == 0
    rows = mig.parse_with_mapping("products", raw, mapping)
    assert rows[0]["name_ar"] == "هاتف"
    assert rows[0]["barcode"] == "1001"


def test_read_sheet(app):
    import io
    from openpyxl import Workbook
    from app.migration import services as mig
    wb = Workbook(); ws = wb.active
    ws.append(["الاسم", "الهاتف"])
    ws.append(["أحمد", "0100"])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    headers, rows = mig.read_sheet(buf)
    assert headers == ["الاسم", "الهاتف"]
    assert rows == [["أحمد", "0100"]]


# --- async report jobs ----------------------------------------------------
def test_async_job_runs(app):
    import time
    from app.core import jobs
    jid = jobs.submit("test", lambda: b"hello-bytes", user_id=_uid(),
                      download_name="x.bin")
    # wait briefly for the daemon thread to finish
    for _ in range(50):
        j = jobs.get(jid)
        if j["status"] != "running":
            break
        time.sleep(0.05)
    j = jobs.get(jid)
    assert j["status"] == "done"
    assert j["path"] is not None
    with open(j["path"], "rb") as fh:
        assert fh.read() == b"hello-bytes"
