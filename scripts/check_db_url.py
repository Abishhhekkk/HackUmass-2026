#!/usr/bin/env python3
"""Show which DATABASE_URL the cloud API will use, where it comes from, and test it.

    python scripts/check_db_url.py

The API reads DATABASE_URL from the process environment, falling back to the repo-root .env
(a value already set in the shell WINS over .env). This prints both, the parsed parts with the
password hidden, and then tries to connect. It never prints the password.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def describe(label: str, url: str | None) -> None:
    if not url:
        print(f"{label}: (not set)")
        return
    try:
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(url)
    except Exception as e:  # noqa: BLE001
        print(f"{label}: could not be parsed ({type(e).__name__}): check its format")
        return
    pw = parts.get("password")
    print(f"{label}:")
    print(f"    user     = {parts.get('user')!r}")
    print(f"    password = {'(missing!)' if not pw else f'(present, {len(pw)} characters)'}")
    print(f"    host     = {parts.get('host')!r}")
    print(f"    port     = {parts.get('port')!r}")
    print(f"    database = {parts.get('dbname')!r}")
    print(f"    sslmode  = {parts.get('sslmode')!r}")
    if pw and any(c in url for c in ("%40", "%3A", "%23")):
        print("    (password contains URL-encoded characters; that is correct for @ : #)")


def main() -> int:
    shell = os.environ.get("DATABASE_URL")
    try:
        from dotenv import dotenv_values
    except ImportError:
        print("pip install python-dotenv (server/requirements.txt)")
        return 1
    env_file = REPO / ".env"
    file_value = dotenv_values(env_file).get("DATABASE_URL") if env_file.is_file() else None

    print(f".env file: {env_file} ({'found' if env_file.is_file() else 'NOT FOUND'})\n")
    describe("DATABASE_URL set in this shell", shell)
    describe("DATABASE_URL in .env", file_value)
    effective = shell or file_value
    print()
    if shell and file_value and shell != file_value:
        print("!! The shell value WINS over .env, and they differ. Remove the shell one:")
        print("   PowerShell:  Remove-Item Env:DATABASE_URL      bash:  unset DATABASE_URL")
        print("   (or open a new terminal), then start the API again.\n")
    if not effective:
        print("No DATABASE_URL anywhere: the API would use local SQLite.")
        return 1
    if not effective.startswith(("postgres://", "postgresql://")):
        print("The value does not start with postgresql:// so the API would use SQLite.")
        return 1

    print("Connecting with the value the API will use ...")
    try:
        import psycopg

        with psycopg.connect(effective, connect_timeout=15) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
            avail = {r[0] for r in conn.execute(
                "SELECT name FROM pg_available_extensions WHERE name IN ('postgis','timescaledb')").fetchall()}
        print(f"CONNECTED: Postgres {version}")
        print(f"    timescaledb installed: {'timescaledb' in exts}; "
              f"postgis installed: {'postgis' in exts} (available: {'postgis' in avail})")
        return 0
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        try:
            from psycopg.conninfo import conninfo_to_dict

            pw = conninfo_to_dict(effective).get("password")
            if pw:
                msg = msg.replace(pw, "***")
        except Exception:  # noqa: BLE001
            pass
        print(f"FAILED: {type(e).__name__}: {msg.strip()[:600]}")
        if "no password supplied" in msg:
            print("-> the URL has no password: postgresql://USER:PASSWORD@HOST:PORT/tsdb?sslmode=require")
        elif "password authentication failed" in msg:
            print("-> wrong password (or user). Reset it in the Tiger console if unsure.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
