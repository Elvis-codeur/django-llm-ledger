import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_ledger.standalone.settings")
os.environ["LLM_LEDGER_DB_NAME"] = ":memory:"
