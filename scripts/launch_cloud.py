#!/usr/bin/env python3
"""Vantage cloud side, one click (the laptop). Started by Vantage-Cloud.bat.

1. Reads .env (DATABASE_URL, INGEST_API_KEY; GEMINI_API_KEY optional). No fallback key.
2. Installs Python packages and rebuilds the website only when they changed.
3. Starts the cloud API on 127.0.0.1:8000 and checks it is using Tiger Data.
4. Starts the public address and records it in Tiger Data so the Jetson launcher finds it:
   ngrok with your free static domain when NGROK_DOMAIN + NGROK_AUTHTOKEN are in .env (the same
   address every time), otherwise a Cloudflare quick tunnel (a new address each start).
5. Opens mission control, keeps both running, restarts the tunnel if it drops (and records
   the new address). Ctrl+C or close the window to stop.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_common import (  # noqa: E402
    ROOT, build_web_if_stale, die, http_json, load_env, pip_install_if_changed, port_in_use,
    publish_cloud_url, real, require, say, step, wait_http,
)

API = "http://127.0.0.1:8000"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "cloudflared" / "cloudflared.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "cloudflared" / "cloudflared.exe",
    ]
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    if winget.is_dir():
        candidates += list(winget.rglob("cloudflared*.exe"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def ensure_cloudflared() -> str:
    exe = find_cloudflared()
    if exe:
        return exe
    if os.name == "nt" and shutil.which("winget"):
        say("   cloudflared not found: installing it with winget...")
        subprocess.run(["winget", "install", "--id", "Cloudflare.cloudflared", "-e",
                        "--accept-source-agreements", "--accept-package-agreements"])
        exe = find_cloudflared()
        if exe:
            return exe
    die("cloudflared is not installed. Windows: winget install --id Cloudflare.cloudflared "
        "(then run this again).")


def pump(proc: subprocess.Popen, tag: str, on_line=None) -> None:
    for line in proc.stdout:
        line = line.rstrip()
        if on_line:
            on_line(line)
        if line:
            say(f"[{tag}] {line}")


class Tunnel:
    """cloudflared quick tunnel; restarted (with a new address) if it exits."""

    def __init__(self, exe: str, database_url: str):
        self.exe, self.database_url = exe, database_url
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self._seen = threading.Event()

    def start(self) -> str:
        self.url = None
        self._seen.clear()
        self.proc = subprocess.Popen(
            [self.exe, "tunnel", "--no-autoupdate", "--url", API],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

        def on_line(line: str) -> None:
            m = TUNNEL_RE.search(line)
            if m and not self.url:
                self.url = m.group(0)
                self._seen.set()

        threading.Thread(target=pump, args=(self.proc, "TUNNEL", on_line), daemon=True).start()
        if not self._seen.wait(60):
            die("cloudflared did not print a trycloudflare.com address within 60 s (see [TUNNEL] lines).")
        say(f"   Tunnel address: {self.url}  - waiting until it answers...")
        if not wait_http(f"{self.url}/api/v1/health", 90):
            say("   The address does not answer yet (DNS can take a minute); recording it anyway.")
        publish_cloud_url(self.database_url, self.url)
        say("   Address recorded in Tiger Data: the Jetson launcher will find it.")
        return self.url


def find_ngrok() -> str | None:
    found = shutil.which("ngrok")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    if winget.is_dir():
        for c in winget.rglob("ngrok.exe"):
            return str(c)
    return None


def ensure_ngrok() -> str:
    exe = find_ngrok()
    if exe:
        return exe
    if os.name == "nt" and shutil.which("winget"):
        say("   ngrok not found: installing it with winget...")
        subprocess.run(["winget", "install", "--id", "ngrok.ngrok", "-e",
                        "--accept-source-agreements", "--accept-package-agreements"])
        exe = find_ngrok()
        if exe:
            return exe
    die("ngrok is not installed. Windows: winget install --id ngrok.ngrok (then run this again).")


class NgrokTunnel(Tunnel):
    """ngrok with the account's free static domain: the same address every time."""

    def __init__(self, exe: str, database_url: str, domain: str):
        super().__init__(exe, database_url)
        self.domain = domain.removeprefix("https://").removeprefix("http://").strip("/")

    def start(self) -> str:
        self.url = f"https://{self.domain}"
        # NGROK_AUTHTOKEN comes from .env (load_env put it in the environment)
        self.proc = subprocess.Popen(
            [self.exe, "http", "8000", f"--url={self.url}", "--log=stdout", "--log-format=logfmt"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=pump, args=(self.proc, "NGROK"), daemon=True).start()
        say(f"   Permanent address: {self.url}  - waiting until it answers...")
        if not wait_http(f"{self.url}/api/v1/health", 60):
            if self.proc.poll() is not None:
                die("ngrok stopped (see the [NGROK] lines): check NGROK_AUTHTOKEN and NGROK_DOMAIN in .env.")
            say("   The address does not answer yet; recording it anyway.")
        publish_cloud_url(self.database_url, self.url)
        say("   Address recorded in Tiger Data (it never changes, so CLOUD_URL on the Jetson also works).")
        return self.url


def main() -> None:
    os.chdir(ROOT)
    say("VANTAGE - cloud side (laptop)")

    step("1/5  Settings (.env)")
    load_env()
    database_url = require("DATABASE_URL", "to store missions in Tiger Data")
    if not database_url.startswith(("postgres://", "postgresql://")):
        die("DATABASE_URL must be the Tiger Data postgres:// address for the Jetson to find this laptop.")
    require("INGEST_API_KEY", "so only your Jetson can upload")
    gemini = real(os.environ.get("GEMINI_API_KEY"))
    say(f"   Tiger Data: set   ingest key: set   Gemini: {'set' if gemini else 'not set (reports use the deterministic fallback)'}")

    step("2/5  Python packages and website")
    python = Path(sys.executable)
    reqs = [ROOT / "server/requirements.txt", ROOT / "server/requirements-postgres.txt"]
    if gemini and (ROOT / "server/requirements-ai.txt").is_file():
        reqs.append(ROOT / "server/requirements-ai.txt")
    pip_install_if_changed(python, reqs, ROOT / ".venv" / "cloud-deps.ok")
    build_web_if_stale()

    step("3/5  Cloud API")
    if port_in_use(8000):
        try:
            info = http_json(f"{API}/api/v1/health")
        except Exception:
            info = None
        die("port 8000 is already in use"
            + (" by another copy of the Vantage API" if info else "")
            + ". Close that window (or the old Vantage.bat), then run this again.")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    api = subprocess.Popen(
        [str(python), "-m", "uvicorn", "rescue_api.app:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT / "server", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=pump, args=(api, "API"), daemon=True).start()
    health = wait_http(f"{API}/api/v1/health", 90)
    if not health:
        api.terminate()
        die("the API did not start (see the [API] lines above).")
    if health.get("db") != "postgres":
        api.terminate()
        die(f"the API is using {health.get('db')}, not Tiger Data: check DATABASE_URL in .env.")
    say("   API running on http://127.0.0.1:8000 with Tiger Data")

    domain = os.environ.get("NGROK_DOMAIN", "")
    if real(domain):
        step("4/5  Public address (ngrok, permanent)")
        if not real(os.environ.get("NGROK_AUTHTOKEN")):
            die("NGROK_DOMAIN is set but NGROK_AUTHTOKEN is missing in .env (ngrok dashboard > Your Authtoken).")
        tunnel = NgrokTunnel(ensure_ngrok(), database_url, domain)
    else:
        step("4/5  Public address (Cloudflare quick tunnel; set NGROK_DOMAIN for a permanent one)")
        tunnel = Tunnel(ensure_cloudflared(), database_url)
    url = tunnel.start()

    step("5/5  Running")
    say(f"""
   Mission control (this laptop): {API}/#/dashboard
   Public address (Jetson, phone): {url}
   The Jetson launcher picks the address up from Tiger Data automatically.
   Keep this window open. Ctrl+C or close it to stop.
""")
    webbrowser.open(f"{API}/#/dashboard")

    try:
        while True:
            time.sleep(3)
            if api.poll() is not None:
                die("the API stopped (see the [API] lines above).")
            if tunnel.proc.poll() is not None:
                say("\n   The tunnel stopped: starting it again...")
                tunnel.start()
                say(f"   New public address: {tunnel.url} (recorded; the Jetson follows it)")
    except KeyboardInterrupt:
        say("\nStopping...")
    finally:
        for p in (tunnel.proc, api):
            if p and p.poll() is None:
                p.terminate()
        for p in (tunnel.proc, api):
            try:
                p and p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
