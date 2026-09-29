"""Excel export for lists and reports (spec G9)."""
from __future__ import annotations

import io
from datetime import date, datetime

from flask import Blueprint, request, send_file
from flask_login import login_required
from openpyxl import Workbook
from openpyxl.styles import Font

from app.core.permissions import requires
from app.extensions import db

bp = Blueprint("exports", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _xlsx(title, headers, rows):
    """Build an .xlsx with numbers kept as numbers (G9). rows = list of lists."""
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        ws.cell(row=1, column=c).font = Font(bold=True)
    for r in rows:
        ws.append([_cell(v) for v in r])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _cell(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _send(buf, name):
    return send_file(buf, as_attachment=True, download_name=f"{name}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _fmt_pdf(v):
    """Human-friendly cell for PDF: numbers get thousands separators."""
    from decimal import Decimal
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return "" if v is None else str(v)


def _deliver(title, headers, rows, name, *, aligns=None, totals=None, meta=None):
    """Deliver a tabular export as Excel (default) or PDF when ``?format=pdf``.

    The very same title/headers/rows drive both formats, so every list and report
    exports identically to a spreadsheet or a print-ready Arabic PDF (G9)."""
    if request.args.get("format") == "pdf":
        from app.core.pdf import build_pdf
        blocks = [{"type": "title", "text": title}]
        pairs = list(meta or [])
        pairs.append(("تاريخ الطباعة", date.today().isoformat()))
        blocks.append({"type": "meta", "pairs": pairs})
        blocks.append({"type": "spacer", "height": 3})
        blocks.append({
            "type": "table", "headers": headers,
            "rows": [[_fmt_pdf(c) for c in r] for r in rows],
            "aligns": aligns,
            "totals": [_fmt_pdf(c) for c in totals] if totals else None,
        })
        pdf = build_pdf(blocks, title=title, landscape=len(headers) > 5)
        return send_file(io.BytesIO(pdf), as_attachment=True,
                         download_name=f"{name}.pdf", mimetype="application/pdf")
    return _send(_xlsx(title, headers, rows), name)


def _range():
    def p(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
    today = date.today()
    return (p(request.args.get("start")) or date(today.year, 1, 1),
            p(request.args.get("end")) or today,
            request.args.get("branch_id", type=int) or None)


# --- customers / suppliers / parties --------------------------------------
@bp.route("/customers")
@requires("pos.sell")
def customers():
    from app.sales.models import Customer
    from app.sales import services
    rows = [[c.name_ar, c.phone, c.type_label, c.currency_code,
             services.ar_balance(c)] for c in db.session.scalars(db.select(Customer))]
    return _deliver("العملاء", ["الاسم", "الهاتف", "النوع", "العملة", "الرصيد"], rows,
                    "customers", aligns=["R", "R", "R", "C", "C"])


@bp.route("/suppliers")
@requires("purchase.invoice.create")
def suppliers():
    from app.purchasing.models import Supplier
    from app.purchasing import services
    rows = [[s.name_ar, s.phone, s.currency_code, services.ap_balance(s)]
            for s in db.session.scalars(db.select(Supplier))]
    return _deliver("الموردون", ["الاسم", "الهاتف", "العملة", "المستحق"], rows,
                    "suppliers", aligns=["R", "R", "C", "C"])


@bp.route("/products")
@requires("accounting.journal.create")
def products():
    from app.inventory.models import Product
    rows = [[p.name_ar, p.barcode, p.category, p.default_price, p.min_price,
             p.valuation_method] for p in db.session.scalars(db.select(Product))]
    return _deliver("المنتجات",
                    ["المنتج", "الباركود", "الفئة", "السعر", "الحد الأدنى", "التقييم"],
                    rows, "products", aligns=["R", "C", "R", "C", "C", "C"])


@bp.route("/parties-summary")
@requires("treasury.transfer")
def parties_summary():
    from app.parties import services
    rows = []
    for row in services.all_parties_summary():
        for s in row["rows"]:
            rows.append([row["party"].name_ar, s["currency"], s["took"],
                         s["returned"], s["spent"], s["remaining"]])
    return _deliver("ملخص الأطراف",
                    ["الطرف", "العملة", "أخذ", "ردّ", "صرف", "المتبقي"], rows,
                    "parties-summary", aligns=["R", "C", "C", "C", "C", "C"])


# --- financial reports ----------------------------------------------------
@bp.route("/trial-balance")
@requires("reports.financial")
def trial_balance():
    from app.accounting.services import trial_balance as tb
    data = tb(branch_id=request.args.get("branch_id", type=int) or None)
    rows = [[r["account"].code, r["account"].name_ar, r["debit"], r["credit"],
             r["balance"], "مدين" if r["side"] == "debit" else "دائن"]
            for r in data["rows"]]
    return _deliver("ميزان المراجعة",
                    ["الرمز", "الحساب", "مدين", "دائن", "الرصيد", "الطرف"], rows,
                    "trial-balance", aligns=["C", "R", "C", "C", "C", "C"])


@bp.route("/sales")
@requires("reports.branch")
def sales():
    from app.reports import services
    start, end, branch_id = _range()
    rows = [[r["product"].name_ar, r["qty"], r["revenue"], r["cogs"], r["margin"]]
            for r in services.sales_by_product(start=start, end=end, branch_id=branch_id)]
    return _deliver("المبيعات والربحية",
                    ["المنتج", "الكمية", "الإيراد", "التكلفة", "هامش الربح"], rows,
                    "sales", aligns=["R", "C", "C", "C", "C"])


@bp.route("/aging")
@requires("purchase.invoice.create")
def aging():
    from app.purchasing import services
    rows = [[r["supplier"].name_ar, r["buckets"]["0-30"], r["buckets"]["31-60"],
             r["buckets"]["61-90"], r["buckets"]["90+"], r["total"]]
            for r in services.aging()]
    return _deliver("أعمار الديون",
                    ["المورد", "0-30", "31-60", "61-90", "+90", "الإجمالي"], rows,
                    "aging", aligns=["R", "C", "C", "C", "C", "C"])


@bp.route("/audit")
@requires("audit.view")
def audit_log():
    from app.core.audit import AuditLog
    rows = [[e.at, e.user_name, e.action, e.entity, e.old_value, e.new_value, e.ip_address]
            for e in db.session.scalars(
                db.select(AuditLog).order_by(AuditLog.at.desc()).limit(5000))]
    return _deliver("سجل التدقيق",
                    ["الوقت", "المستخدم", "العملية", "الكيان", "القديم", "الجديد", "الجهاز"],
                    rows, "audit-log")


@bp.route("/journal")
@requires("accounting.journal.create")
def journal():
    from app.accounting.models import JournalEntry
    rows = [[e.number, e.date, e.memo, e.total_debit_book, e.status]
            for e in db.session.scalars(
                db.select(JournalEntry).order_by(JournalEntry.id.desc()).limit(5000))]
    return _deliver("القيود",
                    ["الرقم", "التاريخ", "البيان", "القيمة", "الحالة"], rows,
                    "journal", aligns=["C", "C", "R", "C", "C"])


# --- statement-style financial reports (PDF-first, also Excel) ------------
@bp.route("/income-statement")
@requires("reports.financial")
def income_statement():
    from app.reports import services
    start, end, branch_id = _range()
    r = services.income_statement(start=start, end=end, branch_id=branch_id)
    rows = [["الإيرادات", ""]]
    rows += [[x["account"].name_ar, x["amount"]] for x in r["revenue"]]
    rows.append(["إجمالي الإيرادات", r["revenue_total"]])
    rows.append(["تكلفة المبيعات", r["cogs"]])
    rows.append(["مجمل الربح", r["gross"]])
    rows.append(["المصروفات", ""])
    rows += [[x["account"].name_ar, x["amount"]] for x in r["expenses"]]
    rows.append(["إجمالي المصروفات", r["expense_total"]])
    return _deliver("قائمة الدخل", ["البند", "القيمة"], rows, "income-statement",
                    aligns=["R", "C"], totals=["صافي الربح", r["net"]],
                    meta=[("من", start.isoformat()), ("إلى", end.isoformat())])


@bp.route("/balance-sheet")
@requires("reports.financial")
def balance_sheet():
    from app.reports import services
    _, end, branch_id = _range()
    r = services.balance_sheet(as_of=end, branch_id=branch_id)
    rows = [["الأصول", ""]]
    rows += [[x["account"].name_ar, x["amount"]] for x in r["assets"]]
    rows.append(["إجمالي الأصول", r["assets_total"]])
    rows.append(["الخصوم", ""])
    rows += [[x["account"].name_ar, x["amount"]] for x in r["liabilities"]]
    rows.append(["إجمالي الخصوم", r["liabilities_total"]])
    rows.append(["حقوق الملكية", ""])
    rows += [[x["account"].name_ar, x["amount"]] for x in r["equity"]]
    rows.append(["صافي ربح الفترة", r["net_income"]])
    rows.append(["إجمالي حقوق الملكية", r["equity_total"]])
    return _deliver("الميزانية العمومية", ["البند", "القيمة"], rows, "balance-sheet",
                    aligns=["R", "C"], meta=[("كما في", end.isoformat())])


@bp.route("/customer/<int:cid>/statement")
@requires("pos.sell")
def customer_statement(cid):
    from app.sales.models import Customer
    from app.reports import services as rs
    c = db.get_or_404(Customer, cid)
    start, end, _ = _range()
    rows = [[m["entry"].date, m["entry"].memo, m["line"].debit_book,
             m["line"].credit_book, m["balance"]]
            for m in rs.general_ledger(c.account_id, start=start, end=end)]
    return _deliver(f"كشف حساب: {c.name_ar}",
                    ["التاريخ", "البيان", "مدين", "دائن", "الرصيد"], rows,
                    f"statement-{cid}", aligns=["C", "R", "C", "C", "C"],
                    meta=[("العميل", c.name_ar), ("الهاتف", c.phone or "—")])
