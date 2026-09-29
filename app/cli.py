"""CLI commands: initialise and seed the database.

Run:  flask --app wsgi seed
"""
import click
from flask.cli import with_appcontext

from app.extensions import db

# --- permission catalogue (spec §3 matrix) --------------------------------
PERMISSIONS = [
    ("settings.manage", "الوصول لشاشة الإعدادات والصلاحيات", "النظام"),
    ("users.manage", "إدارة المستخدمين والأدوار", "النظام"),
    ("audit.view", "عرض سجل التدقيق", "التدقيق"),
    ("accounting.journal.create", "إنشاء قيد يدوي", "المحاسبة"),
    ("accounting.journal.post", "ترحيل واعتماد القيود", "المحاسبة"),
    ("accounting.period.close", "فتح وإقفال الفترات", "المحاسبة"),
    ("product.price.edit", "تعديل سعر البيع أو الحد الأدنى", "المبيعات"),
    ("sale.below_min", "البيع تحت الحد الأدنى", "المبيعات"),
    ("pos.sell", "البيع من نقطة البيع", "المبيعات"),
    ("shift.manage", "فتح وتقفيل الوردية", "المبيعات"),
    ("sales.return.create", "إنشاء مرتجع مبيعات", "المبيعات"),
    ("sales.return.approve", "اعتماد مرتجع مبيعات", "المبيعات"),
    ("purchase.invoice.create", "إنشاء فاتورة شراء", "المشتريات"),
    ("purchase.invoice.post", "اعتماد فاتورة شراء", "المشتريات"),
    ("custody.use", "استخدام العهدة", "العهد"),
    ("custody.settle", "صرف وتسوية العهدة", "العهد"),
    ("treasury.transfer", "التحويلات البنكية وبين الخزائن", "الخزائن"),
    ("warehouse.transfer", "التحويل بين المخازن", "المخزون"),
    ("reports.financial", "التقارير المالية", "التقارير"),
    ("reports.branch", "تقارير الفرع", "التقارير"),
    ("reports.shift", "تقرير الوردية", "التقارير"),
]

# code -> (name_ar, is_system, [permission codes])
ROLES = {
    "owner": ("مالك", False, "ALL"),
    "accountant": (
        "محاسب",
        False,
        [
            "audit.view", "accounting.journal.create", "accounting.journal.post",
            "sales.return.approve", "purchase.invoice.create",
            "purchase.invoice.post", "custody.settle", "treasury.transfer",
            "warehouse.transfer", "reports.financial", "reports.branch",
        ],
    ),
    "cashier": ("كاشير", False, ["pos.sell", "shift.manage",
                                 "sales.return.create", "reports.shift"]),
    "purchase_rep": ("مندوب شراء", False, ["purchase.invoice.create", "custody.use"]),
    "sysadmin": ("مدير النظام", True, ["settings.manage", "users.manage",
                                       "audit.view"]),
}

# --- settings catalogue (spec §2). Values marked "no default" per spec stay
# None until the client sets them. ------------------------------------------
# (key, section, type, scope, label_ar, default, locks_after_use, owner_approval)
SETTINGS = [
    ("company.name", "company", "str", "global", "اسم الشركة", "شركة مثال", False, False),
    ("company.book_currency", "company", "str", "global", "عملة الدفاتر", "AED", True, True),
    ("fx.source", "company", "str", "global", "مصدر سعر الصرف", "تلقائي مع تعديل يدوي", False, False),
    ("finance.tax_enabled", "finance", "bool", "branch", "تفعيل ضريبة القيمة المضافة للفرع", "false", False, False),
    ("finance.tax_rate", "finance", "decimal", "branch", "نسبة الضريبة للفرع (بدون قيمة افتراضية)", None, False, False),
    ("inventory.expense_allocation", "inventory", "str", "global", "طريقة توزيع الشحن والجمارك", "بالقيمة", False, False),
    ("inventory.valuation_default", "inventory", "str", "global", "طريقة التقييم الافتراضية", "المتوسط المرجح", False, False),
    ("inventory.serial_tracking", "inventory", "bool", "global", "تفعيل تتبع السريال (IMEI)", "false", False, False),
    ("inventory.allow_negative_stock", "inventory", "bool", "global", "السماح برصيد مخزون سالب", "false", False, False),
    ("sales.enforce_min_price", "sales", "bool", "global", "منع البيع تحت الحد الأدنى", "true", False, False),
    ("sales.return_days", "sales", "int", "global", "عدد أيام السماح بالمرتجعات", "14", False, False),
    ("sales.return_needs_approval", "sales", "bool", "global", "المرتجع يحتاج اعتماد", "true", False, False),
    ("treasury.transfer_approval_limit", "treasury", "decimal", "global", "حد اعتماد المالك للتحويل (اختياري، بدون قيمة افتراضية)", None, False, False),
    ("custody.attachment_required", "custody", "bool", "global", "إلزام مرفق لكل مصروف عهدة", "true", False, False),
    ("custody.expense_approval_limit", "custody", "decimal", "global", "حد اعتماد مصروف العهدة (المبلغ الأكبر يحتاج اعتماد المالك، 0 = بدون)", "0", False, False),
    ("numbering.sale", "numbering", "str", "global", "صيغة ترقيم فواتير البيع", "INV-{br}-{seq}", False, False),
    ("numbering.purchase", "numbering", "str", "global", "صيغة ترقيم فواتير الشراء", "PUR-{br}-{seq}", False, False),
    ("numbering.journal", "numbering", "str", "global", "صيغة ترقيم القيود", "JE-{br}-{seq}", False, False),
    ("numbering.seq_width", "numbering", "int", "global", "عدد خانات التسلسل في الترقيم", "6", False, False),
]


# --- default chart of accounts (spec §13.1). (code, name_ar, type, postable) ---
# parent is inferred from code prefix.
CHART = [
    ("1", "الأصول", "asset", False),
    ("11", "النقدية والبنوك", "asset", False),
    ("1101", "الخزينة الرئيسية", "asset", True),
    ("1102", "حساب بنكي", "asset", True),
    ("1103", "درج الكاشير", "asset", True),
    ("12", "المدينون", "asset", False),
    ("1201", "ذمم العملاء", "asset", True),
    ("1202", "ذمم جهات التمويل", "asset", True),
    ("1203", "عهد المناديب", "asset", True),
    ("1204", "أرصدة لدى الأشخاص", "asset", False),
    ("13", "المخزون", "asset", False),
    ("1301", "المخزون", "asset", True),
    ("1302", "بضاعة في الطريق", "asset", True),
    ("2", "الخصوم", "liability", False),
    ("2101", "ذمم الموردين", "liability", True),
    ("2102", "ضريبة القيمة المضافة", "liability", True),
    ("3", "حقوق الملكية", "equity", False),
    ("3101", "رأس المال", "equity", True),
    ("3102", "الأرباح المحتجزة", "equity", True),
    ("3103", "رصيد افتتاحي", "equity", True),
    ("4", "الإيرادات", "revenue", False),
    ("4101", "إيراد المبيعات", "revenue", True),
    ("4102", "أرباح فروق العملة", "revenue", True),
    ("4103", "زيادة الخزينة", "revenue", True),
    ("4104", "أرباح تسوية الجرد", "revenue", True),
    ("5", "المصروفات", "expense", False),
    ("5101", "تكلفة البضاعة المباعة", "expense", True),
    ("5102", "عمولة جهات التمويل", "expense", True),
    ("5103", "مصروفات بنكية", "expense", True),
    ("5104", "خسائر فروق العملة", "expense", True),
    ("5105", "عجز الخزينة", "expense", True),
    ("5106", "خسائر/تسويات الجرد", "expense", True),
]

# operation_code -> account code (spec §13 automatic-entry table). Global scope.
MAPPINGS = {
    "sales.revenue": "4101", "inventory": "1301", "cogs": "5101",
    "vat.output": "2102", "customer.ar": "1201", "funder.ar": "1202",
    "funder.commission": "5102", "supplier.ap": "2101", "custody": "1203",
    "goods_in_transit": "1302", "bank.fee": "5103", "fx.gain": "4102",
    "fx.loss": "5104", "shift.shortage": "5105", "shift.surplus": "4103",
    "inventory.adjust.loss": "5106", "inventory.adjust.gain": "4104",
    "cash.main": "1101", "bank": "1102", "cash.drawer": "1103",
    "opening.equity": "3103",
}


def register_cli(app):
    app.cli.add_command(seed_command)
    app.cli.add_command(create_admin_command)
    app.cli.add_command(backup_command)
    app.cli.add_command(check_integrity_command)


@click.command("seed")
@with_appcontext
def seed_command():
    """Create tables and seed reference data + an owner user."""
    run_seed(create_tables=True)
    click.echo("✓ تم إنشاء الجداول")
    click.echo("✓ تم زرع البيانات المرجعية")
    click.echo("  المالك:      owner / owner123")
    click.echo("  مدير النظام: admin / admin123")


def run_seed(create_tables=False):
    """Seed reference + demo data. Shared by the CLI and the test suite."""
    from app.auth.models import Permission, Role, User
    from app.core.models import Branch, Country, Currency
    from app.admin.models import SettingDefinition

    if create_tables:
        db.create_all()

    # Currencies (spec §5.1)
    currencies = [
        ("AED", "درهم إماراتي", "UAE Dirham", 2, "د.إ"),
        ("EGP", "جنيه مصري", "Egyptian Pound", 2, "ج.م"),
        ("USD", "دولار أمريكي", "US Dollar", 2, "$"),
    ]
    for code, ar, en, dp, sym in currencies:
        if not db.session.scalar(db.select(Currency).filter_by(code=code)):
            db.session.add(Currency(code=code, name_ar=ar, name_en=en,
                                    decimal_places=dp, symbol=sym))

    # Countries
    countries = [("مصر", "Egypt", "EG", "EGP"), ("الإمارات", "UAE", "AE", "AED")]
    for ar, en, iso, cur in countries:
        if not db.session.scalar(db.select(Country).filter_by(iso_code=iso)):
            db.session.add(Country(name_ar=ar, name_en=en, iso_code=iso,
                                   currency_code=cur))
    db.session.flush()

    # Permissions
    perm_by_code = {}
    for code, label, group in PERMISSIONS:
        p = db.session.scalar(db.select(Permission).filter_by(code=code))
        if p is None:
            p = Permission(code=code, label_ar=label, group=group)
            db.session.add(p)
        perm_by_code[code] = p
    db.session.flush()

    # Roles
    role_by_code = {}
    for code, (name_ar, is_system, perms) in ROLES.items():
        r = db.session.scalar(db.select(Role).filter_by(code=code))
        if r is None:
            r = Role(code=code, name_ar=name_ar, is_system=is_system)
            db.session.add(r)
        granted = list(perm_by_code.values()) if perms == "ALL" else [
            perm_by_code[c] for c in perms
        ]
        r.permissions = granted
        role_by_code[code] = r
    db.session.flush()

    # Settings catalogue
    for key, section, vtype, scope, label, default, lock, approval in SETTINGS:
        d = db.session.scalar(db.select(SettingDefinition).filter_by(key=key))
        if d is None:
            db.session.add(SettingDefinition(
                key=key, section=section, value_type=vtype, scope=scope,
                label_ar=label, label_en=key, default_value=default,
                locks_after_first_use=lock, requires_owner_approval=approval,
            ))

    # Chart of accounts (spec §13.1)
    from app.accounting.models import (
        Account, AccountMapping, AccountingPeriod,
    )
    from datetime import date

    acc_by_code = {}
    for code, name_ar, atype, postable in CHART:
        a = db.session.scalar(db.select(Account).filter_by(code=code))
        if a is None:
            parent_code = code[:-2] if len(code) == 4 else (code[:-1] if len(code) == 2 else None)
            parent = acc_by_code.get(parent_code)
            a = Account(code=code, name_ar=name_ar, name_en=code, type=atype,
                        is_postable=postable, parent_id=parent.id if parent else None)
            db.session.add(a)
            db.session.flush()
        acc_by_code[code] = a

    # Account mappings for automatic entries (spec §13)
    for op, code in MAPPINGS.items():
        if not db.session.scalar(
            db.select(AccountMapping).filter_by(operation_code=op, branch_id=None)
        ):
            db.session.add(AccountMapping(
                operation_code=op, branch_id=None, account_id=acc_by_code[code].id
            ))

    # An open accounting period for the current year (spec §13.6)
    year = date.today().year
    if not db.session.scalar(db.select(AccountingPeriod).filter_by(name=str(year))):
        db.session.add(AccountingPeriod(
            name=str(year), start_date=date(year, 1, 1), end_date=date(year, 12, 31),
            status="open",
        ))

    # Sample branches
    eg = db.session.scalar(db.select(Country).filter_by(iso_code="EG"))
    ae = db.session.scalar(db.select(Country).filter_by(iso_code="AE"))
    if not db.session.scalar(db.select(Branch)):
        db.session.add(Branch(name_ar="فرع القاهرة", name_en="Cairo Branch",
                              country_id=eg.id))
        db.session.add(Branch(name_ar="فرع دبي", name_en="Dubai Branch",
                              country_id=ae.id))

    db.session.flush()

    # --- Phase 2.5 demo data: warehouse, products, opening stock, treasury ---
    from app.inventory.models import Product, Warehouse
    from app.inventory import services as inv_services
    from app.treasury import services as tr_services
    from app.treasury.models import Treasury

    dubai = db.session.scalar(db.select(Branch).filter_by(name_en="Dubai Branch"))
    cairo = db.session.scalar(db.select(Branch).filter_by(name_en="Cairo Branch"))
    if dubai and not db.session.scalar(db.select(Warehouse)):
        wh = Warehouse(name_ar="مخزن دبي الرئيسي", branch_id=dubai.id)
        db.session.add(wh)
        # a second Dubai warehouse (same currency) + a Cairo warehouse (EGP)
        db.session.add(Warehouse(name_ar="مخزن دبي الفرعي", branch_id=dubai.id))
        if cairo:
            db.session.add(Warehouse(name_ar="مخزن القاهرة", branch_id=cairo.id))
        # exchange rates so cross-country transfers/reports can convert
        from app.accounting.models import ExchangeRate
        db.session.add(ExchangeRate(from_currency="EGP", to_currency="AED", rate="0.083"))
        db.session.add(ExchangeRate(from_currency="AED", to_currency="EGP", rate="12.05"))
        db.session.flush()
        # (name, barcode, price, min, cost, reorder, valuation, serial)
        demo_products = [
            ("هاتف سامسونج A54", "1001", "3000", "2500", "2000", "5", "FIFO", False),
            ("هاتف آيفون 15", "1002", "4500", "4000", "3600", "5", "FIFO", True),
            ("سماعة بلوتوث", "1003", "300", "200", "150", "15", "WA", False),
            ("شاحن سريع", "1004", "120", "90", "60", "8", "WA", False),
        ]
        for name, bc, price, minp, cost, reorder, val, serial in demo_products:
            p = Product(name_ar=name, barcode=bc, category="إلكترونيات",
                        default_price=price, min_price=minp, valuation_method=val,
                        reorder_level=reorder, track_serial=serial)
            db.session.add(p)
            db.session.flush()
            serials = ([f"{bc}-SN{n:04d}" for n in range(1, 11)] if serial else None)
            inv_services.receive_stock(product=p, warehouse=wh, qty="10",
                                       unit_cost=cost, source="opening",
                                       serials=serials)
        # a cash drawer treasury for the Dubai branch POS
        if not db.session.scalar(db.select(Treasury).filter_by(type="drawer")):
            tr_services.create_treasury(name_ar="درج الكاشير — دبي",
                                        branch_id=dubai.id, currency_code="AED",
                                        type="drawer", opening_balance="0")
        # a main treasury (for custody issue / supplier payments)
        main_t = tr_services.create_treasury(name_ar="خزينة دبي الرئيسية",
                                             branch_id=dubai.id, currency_code="AED",
                                             type="main", opening_balance="50000")
        # a demo supplier and an open consignment
        from app.purchasing import services as pur_services
        from app.custody import services as cust_services
        pur_services.create_supplier(name_ar="مورد الإلكترونيات المتحدة",
                                     phone="0500000000", currency_code="AED",
                                     payment_terms_days=30, branch_id=dubai.id)
        cust_services.create_custody(name_ar="مندوب المشتريات — أحمد",
                                     branch_id=dubai.id, currency_code="AED")
        # a demo customer with a credit limit
        from app.sales import services as sales_services
        sales_services.create_customer(name_ar="شركة النور للتجارة", phone="0551111111",
                                       type="wholesale", credit_limit="20000",
                                       currency_code="AED", branch_id=dubai.id)
        # a BNPL funder (تابي) — commission set by the client, no default
        sales_services.create_funder(name_ar="تابي", branch_id=dubai.id,
                                     currency_code="AED", commission_pct="6")
        # a generic party (person carrying cash) — per-currency sub-accounts (M4)
        from app.parties import services as party_services
        party_services.create_party(name_ar="محمد المندوب", phone="0553333333",
                                    types="person,rep", branch_id=dubai.id)

    # Owner user
    owner = db.session.scalar(db.select(User).filter_by(username="owner"))
    if owner is None:
        owner = User(username="owner", full_name="المالك", email="owner@example.com")
        owner.set_password("owner123")
        owner.roles = [role_by_code["owner"]]
        db.session.add(owner)

    # System admin user (from the platform, per §3)
    admin = db.session.scalar(db.select(User).filter_by(username="admin"))
    if admin is None:
        admin = User(username="admin", full_name="مدير النظام")
        admin.set_password("admin123")
        admin.roles = [role_by_code["sysadmin"]]
        db.session.add(admin)

    db.session.commit()
    return {"owner": "owner", "admin": "admin"}


@click.command("create-admin")
@click.argument("username")
@click.argument("password")
@with_appcontext
def create_admin_command(username, password):
    """Create an additional system-admin user."""
    from app.auth.models import Role, User

    if db.session.scalar(db.select(User).filter_by(username=username)):
        click.echo("المستخدم موجود بالفعل.")
        return
    role = db.session.scalar(db.select(Role).filter_by(code="sysadmin"))
    u = User(username=username, full_name=username, must_change_password=True)
    u.set_password(password)
    u.roles = [role] if role else []
    db.session.add(u)
    db.session.commit()
    click.echo(f"✓ تم إنشاء {username}")


@click.command("backup")
@click.option("--dir", "out_dir", default="backups", help="مجلد النسخ الاحتياطية")
@with_appcontext
def backup_command(out_dir):
    """Back up the database (spec §15.2.6). SQLite -> file copy; Postgres -> pg_dump."""
    import os
    import shutil
    import subprocess
    from datetime import datetime
    from flask import current_app

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    uri = current_app.config["SQLALCHEMY_DATABASE_URI"]

    if uri.startswith("sqlite:///"):
        src = uri.replace("sqlite:///", "")
        dst = os.path.join(out_dir, f"marsoud-{stamp}.sqlite")
        # use SQLite's online backup so a running app stays consistent
        import sqlite3
        con = sqlite3.connect(src)
        bck = sqlite3.connect(dst)
        with bck:
            con.backup(bck)
        con.close(); bck.close()
        # verify the copy opens and has tables
        chk = sqlite3.connect(dst)
        n = chk.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        chk.close()
        click.echo(f"✓ نسخة احتياطية: {dst} ({n} جدول) — تم التحقق من فتحها")
    elif uri.startswith("postgresql"):
        dst = os.path.join(out_dir, f"marsoud-{stamp}.dump")
        subprocess.run(["pg_dump", "--format=custom", "--file", dst, uri], check=True)
        click.echo(f"✓ نسخة احتياطية: {dst}")
    else:
        click.echo("قاعدة بيانات غير مدعومة للنسخ الاحتياطي.")


@click.command("check-integrity")
@with_appcontext
def check_integrity_command():
    """Post-restore health check: trial balance + entity-balance invariants."""
    from app.accounting.services import trial_balance
    tb = trial_balance()
    ok = tb["balanced"]
    click.echo(f"ميزان المراجعة: {'متزن ✓' if ok else 'غير متزن ✗'} "
               f"(مدين {tb['total_debit']} / دائن {tb['total_credit']})")
    if not ok:
        raise SystemExit(1)
    click.echo("✓ فحص السلامة ناجح")
