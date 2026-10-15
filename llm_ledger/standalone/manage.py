#!/usr/bin/env python
"""manage.py for running llm_ledger as its own standalone service: `python manage.py runserver`,
`migrate`, `createsuperuser`, etc. -- same as any ordinary Django project, pointed at this app's own
minimal settings module."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_ledger.standalone.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
