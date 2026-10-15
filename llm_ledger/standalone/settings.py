"""Minimal settings for running llm_ledger as its OWN service (not embedded in a bigger project) --
the django-awl `boot_django()` pattern: everything this app needs to run on its own, nothing more.
Override via environment variables for a real deployment; the defaults (SQLite, DEBUG on, a throwaway
SECRET_KEY) are for local/dev use only, per llm_ledger's own README."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = os.environ.get("LLM_LEDGER_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("LLM_LEDGER_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("LLM_LEDGER_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "django_filters",
    "llm_ledger",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "llm_ledger.standalone.urls"

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("LLM_LEDGER_DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("LLM_LEDGER_DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "HOST": os.environ.get("LLM_LEDGER_DB_HOST", ""),
        "PORT": os.environ.get("LLM_LEDGER_DB_PORT", ""),
        "USER": os.environ.get("LLM_LEDGER_DB_USER", ""),
        "PASSWORD": os.environ.get("LLM_LEDGER_DB_PASSWORD", ""),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
}
