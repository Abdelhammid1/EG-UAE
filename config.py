"""Application configuration.

Local development runs on SQLite with zero setup. Production sets DATABASE_URL
to a PostgreSQL instance. The rest of the app is written to be identical on both;
Postgres-only hardening (triggers, REVOKE, real row locks) is layered on in
production migrations while the ORM enforces the same rules everywhere.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")

    # SQLite by default; PostgreSQL when DATABASE_URL is provided.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'marsoud.sqlite'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    BOOK_CURRENCY = os.environ.get("BOOK_CURRENCY", "AED")
    FX_API_URL = os.environ.get("FX_API_URL", "")

    # Security (spec §15.2)
    PERMANENT_SESSION_LIFETIME = 60 * 30  # idle timeout: 30 minutes
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None
    MAX_LOGIN_ATTEMPTS = 5
    LOGIN_LOCKOUT_MINUTES = 15

    # i18n (spec §15.5) — Arabic first, RTL default.
    LANGUAGES = {"ar": "العربية", "en": "English"}
    BABEL_DEFAULT_LOCALE = "ar"
    BABEL_DEFAULT_TIMEZONE = "UTC"


class ProductionConfig(Config):
    SESSION_COOKIE_SECURE = True


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


def get_config():
    env = os.environ.get("FLASK_ENV", "development")
    return ProductionConfig if env == "production" else Config
