"""WSGI entry point for running llm_ledger standalone under gunicorn/uwsgi/etc.:
gunicorn llm_ledger.standalone.wsgi:application"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_ledger.standalone.settings")
application = get_wsgi_application()
