"""Shared helpers for the one-click launchers (launch_cloud.py on the laptop, launch_edge.py on
the Jetson). Standard library only, so it runs before any dependency is installed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = ("replace-with", "your-", "changeme", "<", "xxx")
ENDPOINT_NAME = "cloud_api"


# ------------------------------------------------------------------------------ output
def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n== {msg}")


def die(msg: str) -> None:
    say(f"\nSTOPPED: {msg}")
    sys.exit(1)


# ------------------------------------------------------------------------------ .env
def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    """Parse KEY=value lines (quotes optional, # comments, optional 'export '). Lines that
    cannot be parsed are reported by number only, never printed (they may hold secrets)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            say(f"   note: .env line {n} is not NAME=value and is ignored")
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        values[key] = val
    return values


def load_env() -> dict[str, str]:
    """Read the repo-root .env into os.environ (variables already set in the shell win)."""
    values = read_env()
    for k, v in values.items():
        os.environ.setdefault(k, v)
    return values


def real(value: str | None) -> bool:
    return bool(value) and not any(p in value.lower() for p in PLACEHOLDERS)


def require(name: str, why: str) -> str:
    value = os.environ.get(name, "")
    if not real(value):
        die(f"{name} is missing (or still the example value) in {ROOT / '.env'}.\n"
            f"         It is needed {why}. Add a line {name}=... and run this again.")
    return value


# ------------------------------------------------------------------------------ processes
def run(cmd: list, cwd: Path = ROOT, quiet: bool = False) -> None:
    say(f"   $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([str(c) for c in cmd], cwd=cwd,
                       stdout=subprocess.DEVNULL if quiet else None)
    if r.returncode:
        die(f"this step failed (exit code {r.returncode}); see the output above.")


def pip_install_if_changed(python: Path, requirements: list[Path], marker: Path, extra: list[str] = ()) -> None:
    """pip install only when the requirement files changed since the last successful install."""
    digest = hashlib.sha256()
    for r in requirements:
        digest.update(r.read_bytes())
    for e in extra:
        digest.update(e.encode())
    want = digest.hexdigest()
    if marker.is_file() and marker.read_text().strip() == want:
        say("   Python packages: up to date")
        return
    say("   Installing Python packages (only when requirements change)...")
    cmd = [python, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    for r in requirements:
        cmd += ["-r", r]
    run(cmd + list(extra))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(want)


def http_json(url: str, timeout: float = 4.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_http(url: str, seconds: float) -> dict | None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            return http_json(url)
        except Exception:
            time.sleep(1)
    return None


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------------------ website
def _newest(paths: list[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
        elif p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def build_web_if_stale() -> None:
    """Rebuild web/dist when any website source is newer than the last build."""
    web = ROOT / "web"
    built = web / "dist" / "index.html"
    sources = [web / "src", web / "index.html", web / "package.json", web / "package-lock.json",
               web / "vite.config.js", web / "public"]
    if built.is_file() and built.stat().st_mtime >= _newest(sources):
        say("   Website: up to date")
        return
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        if built.is_file():
            say("   Website: sources changed but npm is not installed; serving the previous build.")
            return
        die("the website is not built and npm is missing. Install Node.js LTS (https://nodejs.org), "
            "reopen the terminal and run this again.")
    modules = web / "node_modules"
    lock = web / "package-lock.json"
    if not modules.is_dir() or (lock.is_file() and lock.stat().st_mtime > modules.stat().st_mtime):
        run([npm, "install", "--no-audit", "--no-fund"], cwd=web)
        modules.touch()
    run([npm, "run", "build"], cwd=web, quiet=True)
    say("   Website: rebuilt")


# ------------------------------------------------------------------------------ cloud address
SCHEMA = """CREATE TABLE IF NOT EXISTS service_endpoints (
    name TEXT PRIMARY KEY, url TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""


def _connect(database_url: str):
    import psycopg  # noqa: PLC0415

    return psycopg.connect(database_url, connect_timeout=8, autocommit=True)


def publish_cloud_url(database_url: str, url: str) -> None:
    """Record the current public address of the cloud API in Tiger Data (laptop side)."""
    with _connect(database_url) as conn:
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO service_endpoints (name, url, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (name) DO UPDATE SET url = EXCLUDED.url, updated_at = now()",
            (ENDPOINT_NAME, url))


def lookup_cloud_url(database_url: str) -> tuple[str, float] | None:
    """(url, age in seconds) of the address the laptop published, or None (Jetson side)."""
    with _connect(database_url) as conn:
        conn.execute(SCHEMA)
        row = conn.execute(
            "SELECT url, EXTRACT(EPOCH FROM now() - updated_at) FROM service_endpoints WHERE name = %s",
            (ENDPOINT_NAME,)).fetchone()
    return (row[0], float(row[1])) if row else None
