"""Users, roles, permissions (spec §3).

Effective permissions = union of the user's roles' permissions PLUS any
individual extra grants (spec §3 rule 1, and the standalone "manager" ability).
A user is scoped to one or more branches (or all). Permissions are enforced
server-side on every request, never only in the UI (§15.2.4).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app
from flask_login import UserMixin
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from app.core.mixins import TimestampMixin
from app.extensions import db, login_manager

# --- association tables ---------------------------------------------------
user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.ForeignKey("user.id"), primary_key=True),
    db.Column("role_id", db.ForeignKey("role.id"), primary_key=True),
)

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.ForeignKey("role.id"), primary_key=True),
    db.Column("permission_id", db.ForeignKey("permission.id"), primary_key=True),
)

# Individual extra grants beyond a user's roles (e.g. the "manager" ability to
# sell below minimum / edit price — a permission, not a sixth role, per §3).
user_permissions = db.Table(
    "user_permissions",
    db.Column("user_id", db.ForeignKey("user.id"), primary_key=True),
    db.Column("permission_id", db.ForeignKey("permission.id"), primary_key=True),
)

user_branches = db.Table(
    "user_branches",
    db.Column("user_id", db.ForeignKey("user.id"), primary_key=True),
    db.Column("branch_id", db.ForeignKey("branch.id"), primary_key=True),
)


class Permission(db.Model):
    __tablename__ = "permission"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(db.String(80), unique=True, index=True)
    label_ar: Mapped[str] = mapped_column(db.String(160))
    group: Mapped[str] = mapped_column(db.String(60), default="عام")

    def __repr__(self):
        return f"<Permission {self.code}>"


class Role(TimestampMixin, db.Model):
    __tablename__ = "role"

    code: Mapped[str] = mapped_column(db.String(40), unique=True, index=True)
    name_ar: Mapped[str] = mapped_column(db.String(80))
    is_system: Mapped[bool] = mapped_column(default=False)

    permissions = db.relationship(
        "Permission", secondary=role_permissions, backref="roles"
    )

    def __repr__(self):
        return f"<Role {self.code}>"


class User(TimestampMixin, UserMixin, db.Model):
    __tablename__ = "user"

    username: Mapped[str] = mapped_column(db.String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(db.String(160))
    email: Mapped[str | None] = mapped_column(db.String(200))
    password_hash: Mapped[str] = mapped_column(db.String(255))

    # 2FA for owner and system admin (spec §15.2.2)
    totp_secret: Mapped[str | None] = mapped_column(db.String(64))
    totp_enabled: Mapped[bool] = mapped_column(default=False)

    # Account lockout (spec §15.2.1)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True)
    must_change_password: Mapped[bool] = mapped_column(default=False)  # SET-01.4

    preferred_locale: Mapped[str] = mapped_column(db.String(5), default="ar")

    roles = db.relationship("Role", secondary=user_roles, backref="users")
    extra_permissions = db.relationship("Permission", secondary=user_permissions)
    branches = db.relationship("Branch", secondary=user_branches)

    # --- password ---
    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    # --- lockout ---
    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > datetime.utcnow())

    def register_failed_login(self):
        self.failed_logins += 1
        if self.failed_logins >= current_app.config["MAX_LOGIN_ATTEMPTS"]:
            self.locked_until = datetime.utcnow() + timedelta(
                minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"]
            )

    def reset_lockout(self):
        self.failed_logins = 0
        self.locked_until = None

    # --- permissions (effective = roles ∪ extra grants) ---
    @property
    def permission_codes(self) -> set[str]:
        codes = set()
        for role in self.roles:
            codes.update(p.code for p in role.permissions)
        codes.update(p.code for p in self.extra_permissions)
        return codes

    def has_permission(self, code: str) -> bool:
        # Owner has everything.
        if any(r.code == "owner" for r in self.roles):
            return True
        return code in self.permission_codes

    def has_role(self, code: str) -> bool:
        return any(r.code == code for r in self.roles)

    @property
    def allowed_branch_ids(self) -> list[int]:
        """Empty list means 'all branches' (spec §16.12)."""
        return [b.id for b in self.branches]

    @property
    def sees_all_branches(self) -> bool:
        return len(self.branches) == 0

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))
