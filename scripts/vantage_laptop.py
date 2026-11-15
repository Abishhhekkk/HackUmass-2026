#!/usr/bin/env python3
"""Vantage laptop launcher: the cloud side in one go (started by Vantage-Laptop.bat).

1. Python environment (.venv) with the cloud API's packages; reinstalls when requirements change.
2. Checks .env: INGEST_API_KEY and DATABASE_URL present, every line readable (values never shown).
3. Builds the website when its source is newer than the last build (needs Node.js).
4. Starts the cloud API on 127.0.0.1:8000 (stops an earlier Vantage API still holding the port).
5. Starts the Cloudflare quick tunnel and waits until the public address answers.
6. Shows the address the Jetson needs (also copied to the clipboard and saved to
   data/cloud/tunnel_url.txt), opens the dashboard, then prints what arrives from the drone.

Ctrl+C (or closing the window) stops the API and the tunnel.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NT = os.name == "nt"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if NT else "bin/python")
API = "http://127.0.0.1:8000"
URL_FILE = ROOT / "data" / "cloud" / "tunnel_url.txt"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ENV_LINE = re.compile(r"^\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
PLACEHOLDERS = {"", "replace-with-a-long-random-secret", "dev-key", "changeme"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n== {msg}")


def die(msg: str) -> None:
    say(f"\nSTOPPED: {msg}")
    sys.exit(1)


def get_json(url: str, timeout: float = 4.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# ------------------------------------------------------------------ 1. environment
def ensure_venv() -> None:
    """Re-run this script inside .venv (created on first use)."""
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    if not VENV_PY.exists():
        step("Creating the Python environment (.venv), first run only")
        import venv

        venv.create(VENV, with_pip=True)
    try:
        sys.exit(subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]]))
    except KeyboardInterrupt:
        sys.exit(0)


def ensure_packages() -> None:
    req = ROOT / "server" / "requirements.txt"
    digest = hashlib.sha256(req.read_bytes()).hexdigest()[:16]
    stamp = VENV / "vantage-server-deps.txt"
    if stamp.exists() and stamp.read_text().strip() == digest:
        return
    step("Installing the cloud API's Python packages")
    if subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)]):
        die("pip install failed (see above). Check the internet connection and try again.")
    stamp.write_text(digest)


# ------------------------------------------------------------------ 2. .env
def check_env() -> None:
    step("Checking .env")
    env_file = ROOT / ".env"
    if not env_file.exists():
        example = ROOT / ".env.example"
        die(f".env not found. Copy {example.name} to .env and fill in DATABASE_URL and INGEST_API_KEY "
            "(docs/deploy.md).")
    names = {}
    bad = []
    for n, line in enumerate(env_file.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = ENV_LINE.match(line)
        if not m:
            bad.append(n)
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        names[m.group(2)] = value
    if bad:
        die(f".env line(s) {', '.join(map(str, bad))} are not NAME=value, so they would be ignored. "
            "Open .env (notepad .env), fix them (no spaces before '=', quotes around values with "
            "spaces or #), save, and start again.")
    key = os.environ.get("INGEST_API_KEY") or names.get("INGEST_API_KEY", "")
    if key in PLACEHOLDERS or len(key) < 16:
        die("INGEST_API_KEY in .env is missing, a placeholder or too short (16+ characters). Make one with:\n"
            "    python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "put it in .env as INGEST_API_KEY=<value>, and use the same value on the Jetson.")
    db = os.environ.get("DATABASE_URL") or names.get("DATABASE_URL", "")
    if not db.startswith(("postgres://", "postgresql://")):
        die("DATABASE_URL in .env is missing or not a postgres:// URL, so data would not go to Tiger Data. "
            "Copy the connection string from the Tiger Data console into .env.")
    if not (os.environ.get("GEMINI_API_KEY") or names.get("GEMINI_API_KEY")):
        say("   note: no GEMINI_API_KEY, so reports use the deterministic fallback (optional).")
    say("   .env OK (INGEST_API_KEY, DATABASE_URL set)")


# ------------------------------------------------------------------ 3. website
def newest(paths) -> float:
    t = 0.0
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    t = max(t, f.stat().st_mtime)
        elif p.exists():
            t = max(t, p.stat().st_mtime)
    return t


def ensure_website() -> None:
    web = ROOT / "web"
    built = web / "dist" / "index.html"
    src = newest([web / "src", web / "index.html", web / "package.json", web / "package-lock.json", web / "public"])
    if built.exists() and built.stat().st_mtime >= src:
        return
    step("Building the website (its source changed)")
    npm = shutil.which("npm")
    if not npm:
        if built.exists():
            say("   Node.js not found: using the existing build, which may be out of date "
                "(install Node.js LTS from nodejs.org to rebuild).")
            return
        die("Node.js is needed to build the website: install the LTS version from https://nodejs.org, "
            "reopen this window and start again.")
    if subprocess.call([npm, "install", "--no-audit", "--no-fund"], cwd=web) or \
            subprocess.call([npm, "run", "build"], cwd=web):
        die("the website build failed (see above).")


# ------------------------------------------------------------------ 4. API
def port_owner_pid(port: int) -> int | None:
    if not NT:
        return None
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            return int(parts[4])
    return None


def free_port() -> None:
    try:
        status = get_json(f"{API}/api/v1/status", 2)
    except Exception:
        status = None
    pid = port_owner_pid(8000)
    if status is None and pid is None:
        return
    if status and status.get("role") == "cloud" and pid:
        say(f"   an earlier Vantage API is still running (process {pid}); stopping it")
        subprocess.call(["taskkill", "/PID", str(pid), "/F"], stdout=subprocess.DEVNULL)
        time.sleep(1.5)
        return
    die(f"port 8000 is used by another program{f' (process {pid})' if pid else ''}. Close it and start again "
        "(Vantage.bat also uses this port).")


def pump(proc, tag: str, sink: list | None = None) -> None:
    for line in proc.stdout:
        line = line.rstrip()
        if sink is not None:
            sink.append(line)
        if tag == "API" and ("GET /api/v1" in line and " 200" in line):
            continue  # the dashboard polls every few seconds; keep the window readable
        if line:
            print(f"   [{tag}] {line}", flush=True)


def start_api() -> subprocess.Popen:
    step("Starting the cloud API")
    free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "rescue_api.app:app", "--host", "127.0.0.1",
                             "--port", "8000"], cwd=ROOT / "server", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=pump, args=(proc, "API"), daemon=True).start()
    for _ in range(60):
        if proc.poll() is not None:
            die("the API stopped while starting (see the [API] lines above).")
        try:
            health = get_json(f"{API}/api/v1/health", 2)
            break
        except Exception:
            time.sleep(0.5)
    else:
        die("the API did not answer within 30 s.")
    if health.get("db") != "postgres":
        die("the API is not using Tiger Data (db = sqlite). DATABASE_URL in .env is not being read.")
    say("   API up on http://127.0.0.1:8000, storing in Tiger Data (postgres)")
    return proc


# ------------------------------------------------------------------ 5. tunnel
def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
                  Path(r"C:\Program Files\cloudflared\cloudflared.exe")]
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    if local.exists():
        candidates += list(local.rglob("cloudflared.exe"))
    return next((str(p) for p in candidates if p.exists()), None)


def start_tunnel() -> tuple[subprocess.Popen, str]:
    step("Starting the Cloudflare tunnel")
    exe = find_cloudflared()
    if not exe and NT and shutil.which("winget"):
        say("   cloudflared not found; installing it with winget")
        subprocess.call(["winget", "install", "--id", "Cloudflare.cloudflared", "-e",
                         "--accept-source-agreements", "--accept-package-agreements"])
        exe = find_cloudflared()
    if not exe:
        die("cloudflared is not installed: winget install --id Cloudflare.cloudflared (then start again).")
    lines: list[str] = []
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", API], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    url = None

    def read():
        nonlocal url
        for line in proc.stdout:
            lines.append(line)
            m = TUNNEL_RE.search(line)
            if m and not url:
                url = m.group(0)
            if " ERR " in line:
                print(f"   [TUNNEL] {line.rstrip()}", flush=True)

    threading.Thread(target=read, daemon=True).start()
    for _ in range(60):
        if url or proc.poll() is not None:
            break
        time.sleep(0.5)
    if not url:
        die("the tunnel did not give an address. Last output:\n" + "".join(lines[-10:]))
    say(f"   address {url}; waiting until it answers (can take ~30 s)")
    for _ in range(45):
        try:
            if get_json(f"{url}/api/v1/health", 5).get("ok"):
                return proc, url
        except Exception:
            time.sleep(2)
    die(f"{url} did not answer within 90 s. Check this laptop's internet connection and start again.")


def publish(url: str) -> None:
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text(url + "\n")
    if NT:
        subprocess.run("clip", input=url, text=True, shell=True)
    bar = "=" * (len(url) + 8)
    say(f"\n{bar}\n    {url}\n{bar}")
    say("   ^ give this address to the Jetson launcher (copied to the clipboard; also saved in "
        "data/cloud/tunnel_url.txt). It changes every time this launcher starts.")


# ------------------------------------------------------------------ 6. watch
def watch(api: subprocess.Popen, tunnel: subprocess.Popen) -> None:
    say("\n== Running. Dashboard: http://localhost:8000/#/dashboard   Stop: Ctrl+C or close this window\n")
    webbrowser.open("http://localhost:8000/#/dashboard")
    last: dict[str, int] | None = None
    while True:
        if api.poll() is not None:
            die("the API stopped (see the [API] lines above).")
        if tunnel.poll() is not None:
            die("the tunnel stopped. Start the launcher again (the address will change).")
        try:
            missions = get_json(f"{API}/api/v1/missions", 4)
        except Exception:
            missions = None
        if missions is not None:
            current = {m["mission_id"]: m.get("latest_version") for m in missions}
            if last is None:
                say(f"   {len(current)} mission(s) already in Tiger Data; waiting for the drone...")
            else:
                for m in missions:
                    mid = m["mission_id"]
                    if last.get(mid) != current[mid]:
                        s = m.get("summary") or {}
                        say(f"   {time.strftime('%H:%M:%S')}  {mid}: snapshot v{current[mid]} stored in Tiger Data"
                            f"  ({s.get('estimated_people', '?')} people placed, "
                            f"{s.get('unlocated_tracks', '?')} not placed)")
            last = current
        time.sleep(3)


def main() -> None:
    os.chdir(ROOT)
    if sys.version_info < (3, 10):
        die("Python 3.10 or newer is needed (python.org).")
    ensure_venv()
    say("Vantage laptop (cloud side)")
    ensure_packages()
    check_env()
    ensure_website()
    api = start_api()
    tunnel = None
    try:
        tunnel, url = start_tunnel()
        publish(url)
        watch(api, tunnel)
    except KeyboardInterrupt:
        say("\nStopping...")
    finally:
        for p in (tunnel, api):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()


if __name__ == "__main__":
    main()
