@python -x "%~f0" %* & pause & exit /b
"""Vantage one-file launcher (double-click on Windows).

Put this file in the repo root. It sets everything up on the first run, then starts the cloud
API and the edge pipeline together and shows both logs, plus a status line, in this window.
Close the window or press Ctrl+C to stop everything.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NT = os.name == "nt"
CLOUD, EDGE = "http://127.0.0.1:8000", "http://127.0.0.1:8080"
MISSION = f"demo-{time.strftime('%H%M%S')}"


def die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def run(cmd, cwd=ROOT):
    print("> " + " ".join(str(c) for c in cmd), flush=True)
    if subprocess.run(cmd, cwd=cwd).returncode:
        die("a setup step failed (see the output above)")


def lan_ip():
    """This machine's address on the local network (what a phone or another laptop would use)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return None


def get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def wait_for(url, name, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            get(url)
            return
        except Exception:
            time.sleep(0.5)
    die(f"{name} did not start within {timeout}s")


def pump(proc, tag):
    for line in proc.stdout:
        print(f"[{tag}] {line.rstrip()}", flush=True)


def main():
    if sys.version_info < (3, 10):
        die("Python 3.10 or newer is required.")
    os.chdir(ROOT)
    for need in ("server/requirements.txt", "edge/requirements.txt", "web/package.json"):
        if not (ROOT / need).exists():
            die(f"{need} not found. Put this file in the repo root (next to README.md).")

    # 1. One-time setup
    vdir = ROOT / ".venv"
    if not vdir.exists():
        print("Creating virtual environment...", flush=True)
        venv.create(vdir, with_pip=True)
    py = vdir / ("Scripts/python.exe" if NT else "bin/python")
    if not (vdir / "deps.ok").exists():
        print("Installing Python dependencies (first run only)...", flush=True)
        run([py, "-m", "pip", "install", "-q", "-r", "server/requirements.txt",
             "-r", "edge/requirements.txt"])
        (vdir / "deps.ok").write_text("ok")
    if not (ROOT / "web/dist/index.html").exists():
        npm = shutil.which("npm")
        if not npm:
            die("Node.js 20+ is required to build the dashboard (https://nodejs.org).")
        print("Building the dashboard (first run only)...", flush=True)
        run([npm, "ci"], cwd=ROOT / "web")
        run([npm, "run", "build"], cwd=ROOT / "web")
    if not (ROOT / "data/demo/detections.jsonl").exists():
        run([py, "scripts/make_demo_data.py"])

    # 2. Start both services together
    env = {**os.environ, "INGEST_API_KEY": os.environ.get("INGEST_API_KEY", "dev-key"),
           "PYTHONPATH": f"{ROOT / 'edge'}{os.pathsep}{ROOT / 'server'}",
           "PYTHONUNBUFFERED": "1"}
    (ROOT / "data/edge/OFFLINE").unlink(missing_ok=True)
    spec = [
        ("CLOUD", [str(py), "-m", "uvicorn", "rescue_api.app:app", "--host", "0.0.0.0", "--port", "8000"], ROOT / "server"),
        ("EDGE ", [str(py), "-m", "rescue_edge.main", "--config", "../config/edge.mock.yaml",
                   "--serve", "--mission-id", MISSION], ROOT / "edge"),
    ]
    procs = []
    for tag, cmd, cwd in spec:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=pump, args=(p, tag), daemon=True).start()
        procs.append(p)

    try:
        wait_for(f"{CLOUD}/api/v1/health", "cloud API")
        wait_for(f"{EDGE}/api/v1/status", "edge pipeline")
        ip = lan_ip()
        print(f"\n=== Running. mission {MISSION}\n"
              f"    edge dashboard : {EDGE}" + (f"   network: http://{ip}:8080" if ip else "") + "\n"
              f"    cloud dashboard: {CLOUD}" + (f"   network: http://{ip}:8000" if ip else "") + "\n"
              "    Other devices on the same Wi-Fi can use the network addresses.\n"
              "    Ctrl+C or close this window to stop.\n", flush=True)
        webbrowser.open(EDGE)
        webbrowser.open(CLOUD)

        # 3. Keep running; print one status line every 5 seconds
        while all(p.poll() is None for p in procs):
            time.sleep(5)
            try:
                st = get(f"{EDGE}/api/v1/status")
                u = st["uploader"]
                try:
                    cloud_v = get(f"{CLOUD}/api/v1/missions/{MISSION}/latest")["manifest"]["version"]
                except Exception:
                    cloud_v = 0
                print(f"[STATUS] frames={st['frames']} detections={st['detections']} "
                      f"edge v{u['local_version']} acked v{u['acked_version']} cloud v{cloud_v} "
                      f"online={u['online']}", flush=True)
            except Exception:
                pass
        print("\nA service stopped unexpectedly; shutting down.")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
