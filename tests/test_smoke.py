"""Smoke tests: prove the foundation's promises hold, not just that pages load."""
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
        _seed_minimal()
        yield app
        db.session.remove()
        db.drop_all()


def _seed_minimal():
    from app.cli import PERMISSIONS, ROLES, SETTINGS
    from app.auth.models import Permission, Role, User
    from app.admin.models import SettingDefinition

    perms = {}
    for code, label, group in PERMISSIONS:
        p = Permission(code=code, label_ar=label, group=group)
        db.session.add(p)
        perms[code] = p
    db.session.flush()
    roles = {}
    for code, (name_ar, is_sys, plist) in ROLES.items():
        r = Role(code=code, name_ar=name_ar, is_system=is_sys)
        r.permissions = list(perms.values()) if plist == "ALL" else [perms[c] for c in plist]
        db.session.add(r)
        roles[code] = r
    for key, section, vtype, scope, label, default, lock, appr in SETTINGS:
        db.session.add(SettingDefinition(
            key=key, section=section, value_type=vtype, scope=scope,
            label_ar=label, label_en=key, default_value=default,
            locks_after_first_use=lock, requires_owner_approval=appr))
    owner = User(username="owner", full_name="المالك")
    owner.set_password("owner123")
    owner.roles = [roles["owner"]]
    admin = User(username="admin", full_name="مدير النظام")
    admin.set_password("admin123")
    admin.roles = [roles["sysadmin"]]
    db.session.add_all([owner, admin])
    db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


# --- money layer ----------------------------------------------------------
def test_money_is_decimal_and_exact(app):
    from app.core.money import Money, quantize_amount
    m = Money("0.1", "AED") + Money("0.2", "AED")
    assert m.amount == Decimal("0.3000")  # no float drift
    assert quantize_amount("10.005") == Decimal("10.0050")


def test_money_refuses_currency_mix(app):
    from app.core.money import Money
    with pytest.raises(ValueError):
        _ = Money("1", "AED") + Money("1", "EGP")


# --- settings registry ----------------------------------------------------
def test_setting_change_appends_and_audits(app):
    from app.core import settings
    from app.core.audit import AuditLog
    settings.set("sales.return_days", "30", user_id=1)
    db.session.commit()
    assert settings.get("sales.return_days") == 30  # cast to int by the registry
    assert db.session.scalar(db.select(db.func.count(AuditLog.id))
                             .filter_by(action="setting.change")) == 1


def test_locked_setting_cannot_change(app):
    from app.core import settings
    settings.lock("company.book_currency")
    db.session.commit()
    with pytest.raises(settings.SettingLockedError):
        settings.set("company.book_currency", "USD", user_id=1)


def test_setting_is_time_aware(app):
    from datetime import datetime, timedelta
    from app.core import settings
    settings.set("sales.return_days", "30", user_id=1)
    db.session.commit()
    past = datetime.utcnow() - timedelta(days=1)
    # before the change, the default applied
    assert settings.get("sales.return_days", at=past) == 14


# --- audit immutability ---------------------------------------------------
def test_audit_log_cannot_be_modified(app):
    from app.core import audit
    from app.core.audit import AuditLog, ImmutableRecordError
    audit.record(action="test.event")
    db.session.commit()
    entry = db.session.scalar(db.select(AuditLog))
    entry.action = "tampered"
    with pytest.raises(ImmutableRecordError):
        db.session.commit()
    db.session.rollback()


# --- permissions ----------------------------------------------------------
def test_owner_has_everything(app):
    from app.auth.models import User
    owner = db.session.scalar(db.select(User).filter_by(username="owner"))
    assert owner.has_permission("settings.manage")
    assert owner.has_permission("anything.at.all")  # owner override


def test_sysadmin_cannot_touch_finance(app):
    from app.auth.models import User
    admin = db.session.scalar(db.select(User).filter_by(username="admin"))
    assert admin.has_permission("settings.manage")
    assert not admin.has_permission("reports.financial")


# --- http flows -----------------------------------------------------------
def test_login_and_dashboard(client):
    r = login(client, "owner", "owner123")
    assert r.status_code == 200
    assert "المالك".encode() in r.data


def test_settings_screen_blocked_for_non_admin(client, app):
    # owner has all permissions incl settings.manage, so use a fresh cashier
    from app.auth.models import Role, User
    cashier = User(username="c1", full_name="كاشير")
    cashier.set_password("x")
    cashier.roles = [db.session.scalar(db.select(Role).filter_by(code="cashier"))]
    db.session.add(cashier)
    db.session.commit()
    login(client, "c1", "x")
    r = client.get("/settings/")
    assert r.status_code == 403


def test_bad_login_rejected(client):
    r = login(client, "owner", "wrong")
    assert "غير صحيحة".encode() in r.data
