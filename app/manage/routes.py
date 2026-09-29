"""Setup & management screens: users, roles/permissions, countries, branches,
currencies, warehouses, account mappings, 2FA (spec M1, M2, INV-02, ACC-02)."""
from __future__ import annotations

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.core import audit
from app.core.permissions import requires
from app.extensions import db

bp = Blueprint("manage", __name__)


@bp.before_request
@login_required
def _guard():
    pass


# --- users (SET-02) -------------------------------------------------------
@bp.route("/users")
@requires("users.manage")
def users():
    from app.auth.models import User
    items = db.session.scalars(db.select(User).order_by(User.username)).all()
    return render_template("manage/users.html", users=items)


@bp.route("/users/new", methods=["GET", "POST"])
@requires("users.manage")
def user_new():
    from app.auth.models import User, Role
    from app.core.models import Branch
    from app.treasury.models import Treasury
    roles = db.session.scalars(db.select(Role).order_by(Role.code)).all()
    branches = db.session.scalars(db.select(Branch)).all()
    treasuries = db.session.scalars(db.select(Treasury)).all()
    if request.method == "POST":
        username = request.form["username"].strip()
        if db.session.scalar(db.select(User).filter_by(username=username)):
            flash("اسم المستخدم مستخدم من قبل.", "error")
            return redirect(request.url)
        u = User(username=username, full_name=request.form["full_name"].strip(),
                 email=request.form.get("email", "").strip() or None,
                 must_change_password=True, created_by_id=current_user.id)
        u.set_password(request.form.get("password") or "temp1234")
        u.roles = [db.session.get(Role, int(r)) for r in request.form.getlist("roles")]
        u.branches = [db.session.get(Branch, int(b)) for b in request.form.getlist("branches")]
        db.session.add(u)
        audit.record(action="user.create", entity="user", entity_id=None,
                     new={"username": username})
        db.session.commit()
        flash("تم إنشاء المستخدم (كلمة مرور مؤقتة، يغيّرها عند أول دخول).", "success")
        return redirect(url_for("manage.users"))
    return render_template("manage/user_form.html", roles=roles, branches=branches,
                           treasuries=treasuries, u=None)


@bp.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@requires("users.manage")
def user_edit(uid):
    from app.auth.models import User, Role, Permission
    from app.core.models import Branch
    u = db.get_or_404(User, uid)
    roles = db.session.scalars(db.select(Role).order_by(Role.code)).all()
    branches = db.session.scalars(db.select(Branch)).all()
    perms = db.session.scalars(db.select(Permission).order_by(Permission.group)).all()
    if request.method == "POST":
        u.full_name = request.form["full_name"].strip()
        u.roles = [db.session.get(Role, int(r)) for r in request.form.getlist("roles")]
        u.branches = [db.session.get(Branch, int(b)) for b in request.form.getlist("branches")]
        u.extra_permissions = [db.session.get(Permission, int(p))
                               for p in request.form.getlist("extra")]
        if request.form.get("reset_password"):
            u.set_password("temp1234")
            u.must_change_password = True
        audit.record(action="user.edit", entity="user", entity_id=u.id)
        db.session.commit()
        flash("تم حفظ المستخدم.", "success")
        return redirect(url_for("manage.users"))
    return render_template("manage/user_form.html", roles=roles, branches=branches,
                           treasuries=[], perms=perms, u=u)


@bp.route("/users/<int:uid>/toggle", methods=["POST"])
@requires("users.manage")
def user_toggle(uid):
    from app.auth.models import User
    u = db.get_or_404(User, uid)
    u.is_active = not u.is_active
    audit.record(action="user.toggle", entity="user", entity_id=u.id,
                 new={"active": u.is_active})
    db.session.commit()
    flash("تم تحديث حالة المستخدم.", "success")
    return redirect(url_for("manage.users"))


# --- permission matrix (SET-03) -------------------------------------------
@bp.route("/roles", methods=["GET", "POST"])
@requires("users.manage")
def roles():
    from app.auth.models import Role, Permission
    all_roles = db.session.scalars(db.select(Role).order_by(Role.code)).all()
    all_perms = db.session.scalars(db.select(Permission).order_by(Permission.group)).all()
    if request.method == "POST":
        for role in all_roles:
            if role.code == "owner":
                continue  # owner keeps everything
            granted = request.form.getlist(f"role_{role.id}")
            role.permissions = [db.session.get(Permission, int(p)) for p in granted]
        audit.record(action="permissions.edit", entity="role", entity_id=None)
        db.session.commit()
        flash("تم حفظ مصفوفة الصلاحيات (تسري على الطلب التالي).", "success")
        return redirect(url_for("manage.roles"))
    role_perms = {r.id: {p.id for p in r.permissions} for r in all_roles}
    groups = {}
    for p in all_perms:
        groups.setdefault(p.group, []).append(p)
    return render_template("manage/roles.html", roles=all_roles, groups=groups,
                           role_perms=role_perms)


# --- simple entity CRUD (SET-05, CUR-01, INV-02) --------------------------
@bp.route("/countries", methods=["GET", "POST"])
@requires("settings.manage")
def countries():
    from app.core.models import Country, Currency
    if request.method == "POST":
        db.session.add(Country(name_ar=request.form["name_ar"].strip(),
                               name_en=request.form.get("name_en", "").strip(),
                               iso_code=request.form["iso_code"].strip().upper(),
                               currency_code=request.form["currency_code"]))
        audit.record(action="country.create", entity="country")
        db.session.commit()
        flash("تمت إضافة الدولة.", "success")
        return redirect(url_for("manage.countries"))
    items = db.session.scalars(db.select(Country)).all()
    currencies = db.session.scalars(db.select(Currency)).all()
    return render_template("manage/countries.html", items=items, currencies=currencies)


@bp.route("/currencies", methods=["GET", "POST"])
@requires("settings.manage")
def currencies():
    from app.core.models import Currency
    if request.method == "POST":
        code = request.form["code"].strip().upper()
        if not db.session.scalar(db.select(Currency).filter_by(code=code)):
            db.session.add(Currency(code=code, name_ar=request.form["name_ar"].strip(),
                                    name_en=request.form.get("name_en", "").strip(),
                                    decimal_places=int(request.form.get("decimal_places") or 2)))
            audit.record(action="currency.create", entity="currency", entity_id=code)
            db.session.commit()
            flash("تمت إضافة العملة.", "success")
        else:
            flash("العملة موجودة.", "warning")
        return redirect(url_for("manage.currencies"))
    items = db.session.scalars(db.select(Currency)).all()
    return render_template("manage/currencies.html", items=items)


@bp.route("/branches", methods=["GET", "POST"])
@requires("settings.manage")
def branches():
    from app.core.models import Branch, Country
    if request.method == "POST":
        db.session.add(Branch(name_ar=request.form["name_ar"].strip(),
                              name_en=request.form.get("name_en", "").strip(),
                              country_id=request.form.get("country_id", type=int),
                              created_by_id=current_user.id))
        audit.record(action="branch.create", entity="branch")
        db.session.commit()
        flash("تمت إضافة الفرع.", "success")
        return redirect(url_for("manage.branches"))
    items = db.session.scalars(db.select(Branch)).all()
    countries = db.session.scalars(db.select(Country)).all()
    return render_template("manage/branches.html", items=items, countries=countries)


@bp.route("/warehouses", methods=["GET", "POST"])
@requires("settings.manage")
def warehouses():
    from app.inventory.models import Warehouse
    from app.core.models import Branch
    if request.method == "POST":
        db.session.add(Warehouse(name_ar=request.form["name_ar"].strip(),
                                 branch_id=request.form.get("branch_id", type=int),
                                 created_by_id=current_user.id))
        audit.record(action="warehouse.create", entity="warehouse")
        db.session.commit()
        flash("تمت إضافة المخزن.", "success")
        return redirect(url_for("manage.warehouses"))
    items = db.session.scalars(db.select(Warehouse)).all()
    branches = db.session.scalars(db.select(Branch)).all()
    return render_template("manage/warehouses.html", items=items, branches=branches)


# --- account mappings (ACC-02) --------------------------------------------
@bp.route("/mappings", methods=["GET", "POST"])
@requires("settings.manage")
def mappings():
    from app.accounting.models import Account, AccountMapping
    all_maps = db.session.scalars(
        db.select(AccountMapping).filter_by(branch_id=None)).all()
    postable = db.session.scalars(
        db.select(Account).filter_by(is_postable=True).order_by(Account.code)).all()
    if request.method == "POST":
        for m in all_maps:
            new_acc = request.form.get(f"map_{m.id}", type=int)
            if new_acc and new_acc != m.account_id:
                m.account_id = new_acc
        audit.record(action="mapping.edit", entity="account_mapping")
        db.session.commit()
        flash("تم حفظ ربط الحسابات.", "success")
        return redirect(url_for("manage.mappings"))
    return render_template("manage/mappings.html", maps=all_maps, accounts=postable)


# --- 2FA (SET-01) ---------------------------------------------------------
@bp.route("/2fa", methods=["GET", "POST"])
def twofa():
    import pyotp
    if request.method == "POST":
        action = request.form.get("action")
        if action == "enable":
            secret = request.form.get("secret")
            code = request.form.get("code", "")
            if pyotp.TOTP(secret).verify(code.strip()):
                current_user.totp_secret = secret
                current_user.totp_enabled = True
                audit.record(action="2fa.enable", entity="user",
                             entity_id=current_user.id)
                db.session.commit()
                flash("تم تفعيل المصادقة الثنائية.", "success")
            else:
                flash("الرمز غير صحيح، حاول مجددًا.", "error")
        elif action == "disable":
            current_user.totp_enabled = False
            current_user.totp_secret = None
            db.session.commit()
            flash("تم إيقاف المصادقة الثنائية.", "success")
        return redirect(url_for("manage.twofa"))
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.username,
                                              issuer_name="Manasety")
    return render_template("manage/twofa.html", secret=secret, uri=uri)
