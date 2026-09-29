"""Migration services: Excel templates, validation, and commit (spec §M13)."""
from __future__ import annotations

import io
import json
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook

from app.core.money import to_decimal
from app.extensions import db

# Column definitions per kind (MIG-02).
KINDS = {
    "products": {
        "label": "المنتجات",
        "columns": ["name_ar", "barcode", "category", "unit", "default_price",
                    "min_price", "valuation_method"],
        "headers": ["اسم المنتج", "الباركود", "الفئة", "الوحدة", "سعر البيع",
                    "الحد الأدنى", "طريقة التقييم (FIFO/WA)"],
    },
    "treasuries": {
        "label": "الخزائن والحسابات",
        "columns": ["name_ar", "type", "branch", "currency", "opening_balance"],
        "headers": ["الاسم", "النوع (main/drawer/bank/wallet)", "الفرع", "العملة",
                    "الرصيد الافتتاحي"],
    },
    "stock": {
        "label": "المخزون الافتتاحي",
        "columns": ["barcode", "warehouse", "qty", "unit_cost", "serials"],
        "headers": ["باركود المنتج", "المخزن", "الكمية", "تكلفة الوحدة",
                    "السريالات (مفصولة بفاصلة)"],
    },
    "customers": {
        "label": "العملاء",
        "columns": ["name_ar", "phone", "type", "currency", "credit_limit",
                    "opening_balance"],
        "headers": ["الاسم", "الهاتف", "النوع (retail/wholesale)", "العملة",
                    "حد الائتمان", "رصيد افتتاحي (مدين)"],
    },
    "suppliers": {
        "label": "الموردون",
        "columns": ["name_ar", "phone", "currency", "payment_terms_days",
                    "opening_balance"],
        "headers": ["الاسم", "الهاتف", "العملة", "مدة السداد", "رصيد افتتاحي (دائن)"],
    },
    "parties": {
        "label": "أرصدة الأشخاص",
        "columns": ["name_ar", "phone", "types", "currency", "opening_balance"],
        "headers": ["الاسم", "الهاتف", "الأنواع", "العملة", "الرصيد الافتتاحي"],
    },
}


# --- template generation (MIG-02) -----------------------------------------
def generate_template(kind) -> io.BytesIO:
    spec = KINDS[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = spec["label"][:31]
    ws.append(spec["headers"])
    for c in range(1, len(spec["headers"]) + 1):
        ws.cell(row=1, column=c).font = ws.cell(row=1, column=c).font.copy(bold=True)

    # a lists sheet of allowed values (MIG-02.2)
    lists = wb.create_sheet("القيم المسموحة")
    from app.core.models import Branch, Currency
    from app.inventory.models import Warehouse
    lists.append(["العملات"] + [c.code for c in db.session.scalars(db.select(Currency))])
    lists.append(["الفروع"] + [b.name_ar for b in db.session.scalars(db.select(Branch))])
    lists.append(["المخازن"] + [w.name_ar for w in db.session.scalars(db.select(Warehouse))])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --- parse + validate (MIG-03) --------------------------------------------
def parse_file(kind, file_storage):
    spec = KINDS[kind]
    wb = load_workbook(file_storage, data_only=True)
    ws = wb.active
    rows = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if row is None or all(v is None for v in row):
            continue
        data = {}
        for j, col in enumerate(spec["columns"]):
            data[col] = row[j] if j < len(row) else None
        data["_line"] = i + 2
        rows.append(data)
    return rows


def read_sheet(file_storage):
    """Read an uploaded sheet's header row and raw data rows (no column
    assumptions) so the user can map their columns to our fields (MIG)."""
    wb = load_workbook(file_storage, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        return [], []
    headers = [("" if h is None else str(h)).strip() for h in all_rows[0]]
    data = [list(r) for r in all_rows[1:] if r and not all(v is None for v in r)]
    return headers, data


def _norm(s) -> str:
    return "".join(str(s or "").split()).lower()


def suggest_mapping(kind, headers):
    """Best-guess {our_column: source_index} by matching the uploaded header text
    to the template headers, falling back to column position."""
    spec = KINDS[kind]
    tmpl, cols = spec["headers"], spec["columns"]
    norm_headers = [_norm(h) for h in headers]
    used = set()
    mapping = {}
    for idx, col in enumerate(cols):
        want = _norm(tmpl[idx]) if idx < len(tmpl) else ""
        src = None
        for j, nh in enumerate(norm_headers):
            if j in used or not nh:
                continue
            if nh == want or (want and (want in nh or nh in want)):
                src = j
                break
        if src is None and idx < len(headers) and norm_headers[idx] \
                and idx not in used:
            src = idx  # positional fallback
        if src is not None:
            used.add(src)
        mapping[col] = src
    return mapping


def parse_with_mapping(kind, raw_rows, mapping):
    """Turn raw rows into field dicts using {our_column: source_index}."""
    spec = KINDS[kind]
    rows = []
    for i, row in enumerate(raw_rows):
        if row is None or all(v is None for v in row):
            continue
        data = {}
        for col in spec["columns"]:
            j = mapping.get(col)
            if isinstance(j, str):
                j = int(j) if j.isdigit() else None
            data[col] = row[j] if (j is not None and j < len(row)) else None
        data["_line"] = i + 2
        rows.append(data)
    return rows


def _num(v):
    if v in (None, ""):
        return None
    try:
        return to_decimal(v)
    except (InvalidOperation, ValueError):
        return "ERR"


def validate(kind, rows):
    """Annotate each row with status (ok/warn/error) + messages (MIG-03)."""
    from app.core.models import Branch, Currency
    from app.inventory.models import Product, Warehouse

    currencies = {c.code for c in db.session.scalars(db.select(Currency))}
    branches = {b.name_ar for b in db.session.scalars(db.select(Branch))}
    warehouses = {w.name_ar: w for w in db.session.scalars(db.select(Warehouse))}
    barcodes = {p.barcode: p for p in db.session.scalars(db.select(Product))
                if p.barcode}
    seen_barcodes = set()
    errors = 0

    for r in rows:
        errs, warns = [], []
        if not r.get("name_ar") and kind != "stock":
            errs.append("الاسم مطلوب")
        cur = (r.get("currency") or "").upper() if "currency" in KINDS[kind]["columns"] else None
        if cur is not None and cur and cur not in currencies:
            errs.append(f"عملة غير معرَّفة: {cur}")

        if kind == "products":
            bc = r.get("barcode")
            if bc and (bc in barcodes or bc in seen_barcodes):
                errs.append(f"باركود مكرر: {bc}")
            if bc:
                seen_barcodes.add(bc)
            for f in ("default_price", "min_price"):
                if _num(r.get(f)) == "ERR":
                    errs.append(f"قيمة غير رقمية: {f}")
        elif kind == "stock":
            p = barcodes.get(r.get("barcode"))
            if p is None:
                errs.append(f"منتج غير موجود بالباركود: {r.get('barcode')}")
            if r.get("warehouse") not in warehouses:
                errs.append(f"مخزن غير معرَّف: {r.get('warehouse')}")
            q = _num(r.get("qty")); c = _num(r.get("unit_cost"))
            if q in (None, "ERR") or (isinstance(q, Decimal) and q < 0):
                errs.append("كمية غير صالحة")
            if c in (None, "ERR") or (isinstance(c, Decimal) and c < 0):
                errs.append("تكلفة غير صالحة")
            if p is not None and p.track_serial and isinstance(q, Decimal):
                serials = [s for s in str(r.get("serials") or "").replace("،", ",").split(",") if s.strip()]
                if len(serials) != int(q):
                    errs.append("عدد السريالات لا يساوي الكمية")
        elif kind == "treasuries":
            if r.get("branch") not in branches:
                errs.append(f"فرع غير معرَّف: {r.get('branch')}")
            if _num(r.get("opening_balance")) == "ERR":
                errs.append("رصيد غير رقمي")

        r["_errors"] = errs
        r["_warnings"] = warns
        r["_status"] = "error" if errs else ("warn" if warns else "ok")
        if errs:
            errors += 1
    return rows, errors


# --- commit (MIG-04) ------------------------------------------------------
def commit(batch, *, user_id=None):
    """Create all entities via the existing services (each posts a balanced
    opening entry). All-or-nothing: any exception rolls the whole batch back."""
    rows = json.loads(batch.rows_json)
    kind = batch.kind
    created = 0

    from app.core.models import Branch
    from app.inventory.models import Product, Warehouse
    from app.inventory import services as inv
    from app.treasury import services as tr
    from app.sales import services as sales
    from app.purchasing import services as pur
    from app.parties import services as parties

    branch = db.session.scalar(db.select(Branch))
    bid = branch.id if branch else None

    for r in rows:
        if kind == "products":
            db.session.add(Product(
                name_ar=r["name_ar"], barcode=r.get("barcode") or None,
                category=r.get("category"), unit=r.get("unit") or "قطعة",
                default_price=r.get("default_price") or 0,
                min_price=r.get("min_price") or 0,
                valuation_method=(r.get("valuation_method") or "FIFO").upper(),
                created_by_id=user_id))
            created += 1
        elif kind == "stock":
            p = db.session.scalar(db.select(Product).filter_by(barcode=r["barcode"]))
            w = db.session.scalar(db.select(Warehouse).filter_by(name_ar=r["warehouse"]))
            serials = [s.strip() for s in str(r.get("serials") or "").replace("،", ",").split(",") if s.strip()] or None
            inv.receive_stock(product=p, warehouse=w, qty=r["qty"],
                              unit_cost=r["unit_cost"], serials=serials,
                              source="opening", user_id=user_id)
            created += 1
        elif kind == "treasuries":
            b = db.session.scalar(db.select(Branch).filter_by(name_ar=r["branch"]))
            typ = (r.get("type") or "main").lower()
            if typ == "bank":
                tr.create_bank_account(name_ar=r["name_ar"], bank_name="",
                                       account_number="", branch_id=b.id,
                                       currency_code=r["currency"],
                                       opening_balance=r.get("opening_balance") or 0,
                                       user_id=user_id)
            else:
                tr.create_treasury(name_ar=r["name_ar"], branch_id=b.id,
                                   currency_code=r["currency"], type=typ,
                                   opening_balance=r.get("opening_balance") or 0,
                                   user_id=user_id)
            created += 1
        elif kind == "customers":
            sales.create_customer(name_ar=r["name_ar"], phone=r.get("phone") or "",
                                  type=(r.get("type") or "retail"),
                                  currency_code=r["currency"],
                                  credit_limit=r.get("credit_limit") or 0,
                                  opening_balance=r.get("opening_balance") or 0,
                                  branch_id=bid, user_id=user_id)
            created += 1
        elif kind == "suppliers":
            pur.create_supplier(name_ar=r["name_ar"], phone=r.get("phone") or "",
                                currency_code=r["currency"],
                                payment_terms_days=int(r.get("payment_terms_days") or 0),
                                opening_balance=r.get("opening_balance") or 0,
                                branch_id=bid, user_id=user_id)
            created += 1
        elif kind == "parties":
            p = parties.create_party(name_ar=r["name_ar"], phone=r.get("phone") or "",
                                     types=r.get("types") or "person", branch_id=bid,
                                     user_id=user_id)
            parties.opening_balance(party=p, currency_code=r["currency"],
                                    amount=r.get("opening_balance") or 0, user_id=user_id)
            created += 1

    from datetime import datetime
    batch.status = "committed"
    batch.committed_at = datetime.utcnow()
    from app.core import audit
    audit.record(action="migration.commit", entity="import_batch",
                 entity_id=batch.id, new={"kind": kind, "created": created})
    db.session.flush()
    return created
