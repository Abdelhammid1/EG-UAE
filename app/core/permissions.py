"""Server-side permission enforcement (spec §3, §15.2.4).

Permissions are checked on the server for every protected handler. The UI
hides buttons only as a courtesy; this is the real control.
"""
from functools import wraps

from flask import abort, g
from flask_login import current_user


def requires(*permission_codes):
    """Handler decorator: require ALL listed permission codes."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            for code in permission_codes:
                if not current_user.has_permission(code):
                    abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def requires_role(*role_codes):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not any(current_user.has_role(r) for r in role_codes):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def can(code: str) -> bool:
    """Template helper: does the current user hold this permission?"""
    return current_user.is_authenticated and current_user.has_permission(code)


def branch_filter(query, model):
    """Restrict a query to branches the current user may see (spec §15.2.5)."""
    if not current_user.is_authenticated or current_user.sees_all_branches:
        return query
    return query.filter(model.branch_id.in_(current_user.allowed_branch_ids))
