"""Treasury & bank screens (spec §6)."""
from __future__ import annotations

from datetime import date, datetime

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.accounting.models import Account, JournalLine
from app.core import fx
from app.core.models import Branch, Currency
from app.core.permissions import requires
from app.extensions import db
from app.treasury import services
from app.treasury.forms import BankForm, TreasuryForm
from app.treasury.models import BankAccount, ReconItem, Treasury, TreasuryTxn

bp = Blueprint("treasury", __name__)


@bp.before_request
@login_required
def _guard():
    pass


def _entities():
    treasuries = db.session.scalars(db.select(Treasury)).all()
    banks = db.session.scalars(db.select(BankAccount)).all()
    return treasuries, banks


def _resolve(kind, eid):
    model = Treasury if kind == "treasury" else BankAccount
    return db.get_or_404(model, eid)


# --- overview -------------------------------------------------------------
@bp.route("/")
@requires("treasury.transfer")
def index():
    treasuries, banks = _entities()
    def bal(e):
        return services.balance_native(e.account_id)
    pending = db.session.scalars(
        db.select(TreasuryTxn).filter_by(status="pending_approval")
    ).all()
    return render_template(
        "treasury/index.html", treasuries=treasuries, banks=banks,
        balance=bal, pending=pending,
    )


# --- create entities ------------------------------------------------------
def _fill_choices(form):
    form.branch_id.choices = [
        (b.id, b.name_ar) for b in db.session.scalars(db.select(Branch))
    ]
    form.currency_code.choices = [
        (c.code, f"{c.code} · {c.name_ar}")
        for c in db.session.scalars(db.select(Currency))
    ]


@bp.route("/treasuries/new", methods=["GET", "POST"])
@requires("treasury.transfer")
def treasury_new():
    form = TreasuryForm()
    _fill_choices(form)
    if form.validate_on_submit():
        services.create_treasury(
            name_ar=form.name_ar.data.strip(), branch_id=form.branch_id.data,
            currency_code=form.currency_code.data, type=form.type.data,
            opening_balance=form.opening_balance.data or 0, user_id=current_user.id,
        )
        db.session.commit()
        flash("تم إنشاء الخزينة.", "success")
        return redirect(url_for("treasury.index"))
    return render_template("treasury/entity_form.html", form=form,
                           title="خزينة جديدة", kind="treasury")


@bp.route("/banks/new", methods=["GET", "POST"])
@requires("treasury.transfer")
def bank_new():
    form = BankForm()
    _fill_choices(form)
    if form.validate_on_submit():
        services.create_bank_account(
            name_ar=form.name_ar.data.strip(), bank_name=form.bank_name.data or "",
            account_number=form.account_number.data or "",
            branch_id=form.branch_id.data, currency_code=form.currency_code.data,
            opening_balance=form.opening_balance.data or 0, user_id=current_user.id,
        )
        db.session.commit()
        flash("تم إنشاء الحساب البنكي.", "success")
        return redirect(url_for("treasury.index"))
    return render_template("treasury/entity_form.html", form=form,
                           title="حساب بنكي جديد", kind="bank")


# --- statement ------------------------------------------------------------
@bp.route("/<kind>/<int:eid>")
@requires("treasury.transfer")
def statement(kind, eid):
    entity = _resolve(kind, eid)
    rows = services.statement(entity.account_id)
    bal = services.balance_native(entity.account_id)
    return render_template("treasury/statement.html", entity=entity, kind=kind,
                           rows=rows, balance=bal)


# --- deposit / withdraw ---------------------------------------------------
@bp.route("/<kind>/<int:eid>/move", methods=["GET", "POST"])
@requires("treasury.transfer")
def move(kind, eid):
    entity = _resolve(kind, eid)
    postable = db.session.scalars(
        db.select(Account).filter_by(is_postable=True, is_active=True).order_by(Account.code)
    ).all()
    if request.method == "POST":
        op = request.form.get("op")
        counter = request.form.get("counter_account_id", type=int)
        amount = request.form.get("amount")
        memo = request.form.get("memo", "").strip()
        try:
            d = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            d = date.today()
        if not counter or not amount:
            flash("أكمل البيانات.", "error")
            return redirect(request.url)
        try:
            if op == "deposit":
                services.deposit(entity=entity, counter_account_id=counter,
                                 amount=amount, entry_date=d, memo=memo,
                                 user_id=current_user.id)
            else:
                services.withdraw(entity=entity, counter_account_id=counter,
                                  amount=amount, entry_date=d, memo=memo,
                                  user_id=current_user.id)
            db.session.commit()
            flash("تم تسجيل الحركة وقيدها.", "success")
            return redirect(url_for("treasury.statement", kind=kind, eid=eid))
        except fx.RateUnavailableError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("treasury/move.html", entity=entity, kind=kind,
                           accounts=postable, today=date.today().isoformat())


# --- internal / fx transfer ----------------------------------------------
@bp.route("/transfer", methods=["GET", "POST"])
@requires("treasury.transfer")
def transfer():
    treasuries, banks = _entities()
    all_entities = [("treasury", t) for t in treasuries] + [("bank", b) for b in banks]
    if request.method == "POST":
        src = _resolve(*request.form["src"].split(":"))
        dst = _resolve(*request.form["dst"].split(":"))
        memo = request.form.get("memo", "").strip()
        try:
            d = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            d = date.today()
        try:
            if src.currency_code == dst.currency_code:
                txn = services.internal_transfer(
                    src=src, dst=dst, amount=request.form["amount"], entry_date=d,
                    memo=memo, user_id=current_user.id, user=current_user)
            else:
                txn = services.fx_transfer(
                    src=src, dst=dst, sent_amount=request.form["amount"],
                    received_amount=request.form["received_amount"],
                    src_rate=request.form.get("src_rate") or None,
                    dst_rate=request.form.get("dst_rate") or None,
                    bank_fee=request.form.get("bank_fee") or 0,
                    entry_date=d, memo=memo, user_id=current_user.id,
                    user=current_user)
            db.session.commit()
            if txn.status == "pending_approval":
                flash("التحويل يتجاوز حد الاعتماد — بانتظار موافقة المالك.", "warning")
            else:
                flash("تم تنفيذ التحويل وقيده.", "success")
            return redirect(url_for("treasury.index"))
        except (fx.RateUnavailableError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("treasury/transfer.html", entities=all_entities,
                           book_currency=_book(), today=date.today().isoformat())


@bp.route("/pending/<int:txn_id>/approve", methods=["POST"])
@requires("treasury.transfer")
def approve(txn_id):
    if not current_user.has_role("owner"):
        abort(403)  # only the owner approves over-limit transfers (§6)
    txn = db.get_or_404(TreasuryTxn, txn_id)
    services.approve_and_post(txn, user_id=current_user.id)
    db.session.commit()
    flash("تم اعتماد التحويل وترحيله.", "success")
    return redirect(url_for("treasury.index"))


# --- deactivate -----------------------------------------------------------
@bp.route("/<kind>/<int:eid>/deactivate", methods=["POST"])
@requires("treasury.transfer")
def deactivate(kind, eid):
    entity = _resolve(kind, eid)
    try:
        services.deactivate(entity, user_id=current_user.id)
        db.session.commit()
        flash("تم التعطيل.", "success")
    except services.BalanceNotZeroError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("treasury.index"))


# --- bank reconciliation --------------------------------------------------
@bp.route("/banks/<int:eid>/reconcile", methods=["GET", "POST"])
@requires("treasury.transfer")
def reconcile(eid):
    bank = db.get_or_404(BankAccount, eid)
    if request.method == "POST":
        line_id = request.form.get("line_id", type=int)
        existing = db.session.scalar(
            db.select(ReconItem).filter_by(bank_account_id=eid, journal_line_id=line_id)
        )
        if existing:
            db.session.delete(existing)
        else:
            db.session.add(ReconItem(
                bank_account_id=eid, journal_line_id=line_id,
                reconciled_by_id=current_user.id,
                statement_ref=request.form.get("statement_ref", "")))
        db.session.commit()
        return redirect(url_for("treasury.reconcile", eid=eid))

    if request.method == "GET" and request.args.get("import") == "1":
        pass  # placeholder; import handled by its own route below
    rows = services.statement(bank.account_id)
    cleared_ids = {
        r.journal_line_id for r in db.session.scalars(
            db.select(ReconItem).filter_by(bank_account_id=eid))
    }
    from decimal import Decimal
    cleared_balance = sum(
        (r["delta"] for r in rows if r["line"].id in cleared_ids), Decimal("0")
    )
    book_balance = services.balance_native(bank.account_id)
    return render_template("treasury/reconcile.html", bank=bank, rows=rows,
                           cleared_ids=cleared_ids, cleared_balance=cleared_balance,
                           book_balance=book_balance)


@bp.route("/banks/<int:eid>/reconcile/import", methods=["POST"])
@requires("treasury.transfer")
def reconcile_import(eid):
    bank = db.get_or_404(BankAccount, eid)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("اختر ملف كشف الحساب (Excel).", "error")
        return redirect(url_for("treasury.reconcile", eid=eid))
    try:
        matched, unmatched = services.import_statement(bank, file,
                                                       user_id=current_user.id)
        db.session.commit()
        flash(f"تمت المطابقة الآلية: {matched} حركة مطابقة، {unmatched} غير مطابقة.",
              "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذّر قراءة الملف: {e}", "error")
    return redirect(url_for("treasury.reconcile", eid=eid))


def _book():
    from flask import current_app
    return current_app.config["BOOK_CURRENCY"]
