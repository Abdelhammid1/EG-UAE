"""Security & permission enforcement (spec §15.2, G2, G10)."""
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
        _cashier()
        yield app
        db.session.remove()
        db.drop_all()


def _cashier():
    from app.auth.models import User, Role
    u = User(username="cash1", full_name="كاشير"); u.set_password("x")
    u.roles = [db.session.scalar(db.select(Role).filter_by(code="cashier"))]
    db.session.add(u); db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client, u, p):
    return client.post("/login", data={"username": u, "password": p},
                       follow_redirects=True)


# --- authentication -------------------------------------------------------
def test_unauthenticated_redirects_to_login(client):
    r = client.get("/reports/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_password_is_hashed_not_plaintext(app):
    from app.auth.models import User
    owner = db.session.scalar(db.select(User).filter_by(username="owner"))
    assert owner.password_hash != "owner123"
    assert owner.check_password("owner123")
    assert "owner123" not in owner.password_hash


def test_account_lockout_after_failed_attempts(app):
    from app.auth.models import User
    owner = db.session.scalar(db.select(User).filter_by(username="owner"))
    for _ in range(5):
        owner.register_failed_login()
    assert owner.is_locked is True


# --- server-side permission enforcement (G2) ------------------------------
def test_cashier_blocked_from_settings(client):
    _login(client, "cash1", "x")
    assert client.get("/settings/").status_code == 403


def test_cashier_blocked_from_financial_reports(client):
    _login(client, "cash1", "x")
    assert client.get("/reports/income-statement").status_code == 403
    assert client.get("/reports/balance-sheet").status_code == 403


def test_cashier_blocked_from_journal(client):
    _login(client, "cash1", "x")
    assert client.get("/accounting/journal").status_code == 403


def test_cashier_can_reach_pos(client):
    _login(client, "cash1", "x")
    assert client.get("/inventory/pos").status_code == 200


def test_direct_post_to_protected_route_rejected(client):
    """Even a crafted direct request is refused server-side (§15.2.4)."""
    _login(client, "cash1", "x")
    r = client.post("/accounting/periods/1/close", data={})
    assert r.status_code == 403


# --- audit log immutability (§15.1) ---------------------------------------
def test_audit_log_is_append_only(app):
    from app.core import audit
    from app.core.audit import AuditLog, ImmutableRecordError
    audit.record(action="test.x")
    db.session.commit()
    row = db.session.scalar(db.select(AuditLog))
    row.action = "tampered"
    with pytest.raises(ImmutableRecordError):
        db.session.commit()
    db.session.rollback()
    # and it cannot be deleted either
    db.session.delete(row)
    with pytest.raises(ImmutableRecordError):
        db.session.commit()
    db.session.rollback()
