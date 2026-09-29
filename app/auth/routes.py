"""Login, logout, 2FA, lockout, language switch (spec §15.2)."""
import pyotp
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.forms import LoginForm
from app.auth.models import User
from app.core import audit
from app.extensions import db

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.home"))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(
            db.select(User).filter_by(username=form.username.data.strip())
        )

        if user is None or not user.check_password(form.password.data):
            if user is not None:
                user.register_failed_login()
                audit.record(action="auth.login_failed", entity="user",
                             entity_id=user.id)
                db.session.commit()
            flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
            return render_template("auth/login.html", form=form)

        if user.is_locked:
            flash("تم قفل الحساب مؤقتًا بعد عدة محاولات فاشلة. حاول لاحقًا.", "error")
            return render_template("auth/login.html", form=form)

        if not user.is_active:
            flash("هذا الحساب موقوف.", "error")
            return render_template("auth/login.html", form=form)

        # 2FA for accounts that require it (spec §15.2.2)
        if user.totp_enabled:
            if not form.otp.data:
                flash("أدخل رمز المصادقة الثنائية.", "warning")
                return render_template("auth/login.html", form=form)
            if not pyotp.TOTP(user.totp_secret).verify(form.otp.data.strip()):
                user.register_failed_login()
                db.session.commit()
                flash("رمز المصادقة الثنائية غير صحيح.", "error")
                return render_template("auth/login.html", form=form)

        user.reset_lockout()
        login_user(user, remember=form.remember.data)
        session["locale"] = user.preferred_locale
        audit.record(action="auth.login", entity="user", entity_id=user.id)
        db.session.commit()
        return redirect(request.args.get("next") or url_for("main.home"))

    return render_template("auth/login.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    audit.record(action="auth.logout", entity="user", entity_id=current_user.id)
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("كلمة المرور الحالية غير صحيحة.", "error")
        elif len(new) < 8:
            flash("كلمة المرور الجديدة يجب ألا تقل عن 8 أحرف.", "error")
        elif new != confirm:
            flash("كلمتا المرور غير متطابقتين.", "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            audit.record(action="auth.password_change", entity="user",
                         entity_id=current_user.id)
            db.session.commit()
            flash("تم تغيير كلمة المرور.", "success")
            return redirect(url_for("main.home"))
    return render_template("auth/change_password.html",
                           forced=current_user.must_change_password)


@bp.route("/set-language/<lang>")
def set_language(lang):
    if lang in ("ar", "en"):
        session["locale"] = lang
        if current_user.is_authenticated:
            current_user.preferred_locale = lang
            db.session.commit()
    return redirect(request.referrer or url_for("main.home"))
