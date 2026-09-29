"""Application factory."""
from flask import Flask, g, request, session
from flask_login import current_user

from config import get_config
from app.extensions import babel, csrf, db, login_manager, migrate


def create_app(config_class=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class or get_config())

    # --- extensions ---
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    babel.init_app(app, locale_selector=_select_locale)

    # --- models must be imported so migrations/create_all see them ---
    from app.core import models as core_models  # noqa: F401
    from app.auth import models as auth_models  # noqa: F401
    from app.admin import models as admin_models  # noqa: F401
    from app.accounting import models as accounting_models  # noqa: F401
    from app.treasury import models as treasury_models  # noqa: F401
    from app.inventory import models as inventory_models  # noqa: F401
    from app.purchasing import models as purchasing_models  # noqa: F401
    from app.custody import models as custody_models  # noqa: F401
    from app.shipping import models as shipping_models  # noqa: F401
    from app.sales import models as sales_models  # noqa: F401
    from app.parties import models as parties_models  # noqa: F401
    from app.migration import models as migration_models  # noqa: F401
    from app.core.audit import AuditLog  # noqa: F401

    # --- ORM immutability guards (spec §15.1, §13.5) ---
    from app.core import audit
    audit.register_guards(app)

    # --- blueprints ---
    from app.auth.routes import bp as auth_bp
    from app.admin.routes import bp as admin_bp
    from app.main.routes import bp as main_bp
    from app.accounting.routes import bp as accounting_bp
    from app.treasury.routes import bp as treasury_bp
    from app.inventory.routes import bp as inventory_bp
    from app.purchasing.routes import bp as purchasing_bp
    from app.custody.routes import bp as custody_bp
    from app.shipping.routes import bp as shipping_bp
    from app.sales.routes import bp as sales_bp
    from app.reports.routes import bp as reports_bp
    from app.parties.routes import bp as parties_bp
    from app.migration.routes import bp as migration_bp
    from app.manage.routes import bp as manage_bp
    from app.exports.routes import bp as exports_bp
    from app.approvals.routes import bp as approvals_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/settings")
    app.register_blueprint(accounting_bp, url_prefix="/accounting")
    app.register_blueprint(treasury_bp, url_prefix="/treasury")
    app.register_blueprint(inventory_bp, url_prefix="/inventory")
    app.register_blueprint(purchasing_bp, url_prefix="/purchasing")
    app.register_blueprint(custody_bp, url_prefix="/custody")
    app.register_blueprint(shipping_bp, url_prefix="/shipping")
    app.register_blueprint(sales_bp, url_prefix="/sales")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(parties_bp, url_prefix="/parties")
    app.register_blueprint(migration_bp, url_prefix="/migration")
    app.register_blueprint(manage_bp, url_prefix="/manage")
    app.register_blueprint(exports_bp, url_prefix="/export")
    app.register_blueprint(approvals_bp, url_prefix="/approvals")
    app.register_blueprint(main_bp)

    # --- force temp-password change on first login (SET-01.4) ---
    @app.before_request
    def _force_password_change():
        from flask import request, redirect, url_for
        if not current_user.is_authenticated:
            return
        if not getattr(current_user, "must_change_password", False):
            return
        allowed = {"auth.change_password", "auth.logout", "static",
                   "auth.set_language"}
        if request.endpoint not in allowed:
            return redirect(url_for("auth.change_password"))

    # --- template helpers ---
    _register_template_helpers(app)

    # --- CLI ---
    from app.cli import register_cli
    register_cli(app)

    return app


def _select_locale():
    if current_user.is_authenticated and getattr(current_user, "preferred_locale", None):
        return current_user.preferred_locale
    return session.get("locale") or request.accept_languages.best_match(["ar", "en"]) or "ar"


def _register_template_helpers(app):
    from app.core.permissions import can
    from app.core.money import round_to_currency
    from flask_babel import get_locale

    @app.context_processor
    def inject_helpers():
        locale = str(get_locale() or "ar")
        return {
            "can": can,
            "locale": locale,
            "text_dir": "rtl" if locale == "ar" else "ltr",
            "current_user": current_user,
            "pending_approvals": _pending_approvals_count,
        }

    def _pending_approvals_count():
        """Lazy count for the sidebar badge — only queried when authenticated."""
        try:
            if not getattr(current_user, "is_authenticated", False):
                return 0
            from app.approvals import services as approval_services
            return approval_services.pending_count()
        except Exception:  # noqa: BLE001 - the badge must never break a page
            return 0

    @app.template_filter("money")
    def money_filter(value, decimal_places=2):
        if value is None:
            return "—"
        d = round_to_currency(value, decimal_places)
        return f"{d:,.{decimal_places}f}"
