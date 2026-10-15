"""Programmatic equivalent of manage.py, for a script/test that wants llm_ledger's standalone Django
instance configured in-process (the django-awl boot_django() pattern) rather than shelling out."""

import os

import django


def boot_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_ledger.standalone.settings")
    django.setup()
