"""Test-wide setup, loaded by pytest before any test module.

rescue_api.app builds an app at import time for `uvicorn rescue_api.app:app`, reading the
repo-root .env. Tests create their own apps against throwaway databases, so switch that off:
a developer's .env (e.g. a real Tiger Data DATABASE_URL) must never be touched by tests.
"""
import os

os.environ["RESCUE_API_NO_AUTOCREATE"] = "1"
