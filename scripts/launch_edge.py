#!/usr/bin/env python3
"""Vantage drone side, one click (the Jetson). Started by vantage-edge.sh.

    ./vantage-edge.sh                         # newest simulated-clip config (config/*.sim.yaml)
    ./vantage-edge.sh --video data/footage/DJI_0001.MP4   # .SRT next to it => real flight log
    ./vantage-edge.sh --video clip.mp4         # no .SRT => simulated flight path (labelled)
    ./vantage-edge.sh --update                 # git pull first
    ./vantage-edge.sh --install-shortcut       # put a Vantage icon on the desktop

1. Reads .env (INGEST_API_KEY; DATABASE_URL to find the laptop; CLOUD_URL to force an address).
2. Uses the trained model (models/person-visdrone.engine; builds it from the .pt if missing)
   and the camera-motion-compensated tracker.
3. Finds the cloud address the laptop launcher recorded in Tiger Data and keeps following it
   (a restarted tunnel is picked up mid-mission). No address => runs offline; uploads wait.
4. Rebuilds the website if it changed, runs the mission and serves http://localhost:8080.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_common import (  # noqa: E402
    ROOT, build_web_if_stale, die, http_json, load_env, lookup_cloud_url, port_in_use, real,
    require, run, say, step, wait_http,
)

RUN_CONFIG = ROOT / "config" / "launcher-run.sim.yaml"   # *.sim.yaml is gitignored
URL_FILE = ROOT / "data" / "edge" / "cloud_url.txt"
OFFLINE_FLAG = ROOT / "data" / "edge" / "OFFLINE"
ENGINE = ROOT / "models" / "person-visdrone.engine"
WEIGHTS = ROOT / "models" / "person-visdrone.pt"
EDGE = "http://127.0.0.1:8080"


def install_shortcut() -> None:
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    script = ROOT / "vantage-edge.sh"
    entry = desktop / "Vantage-Edge.desktop"
    entry.write_text(f"""[Desktop Entry]
Type=Application
Name=Vantage (drone side)
Comment=Run the Vantage edge pipeline and dashboard
Exec=bash -c '"{script}"; echo; read -p "Press Enter to close"'
Path={ROOT}
Terminal=true
Icon=applications-science
""")
    entry.chmod(0o755)
    subprocess.run(["gio", "set", str(entry), "metadata::trusted", "true"], stderr=subprocess.DEVNULL)
    say(f"Desktop icon created: {entry}\nDouble-click it (first time: right-click > Allow Launching).")


def choose_config(args) -> Path:
    if args.config:
        return Path(args.config).resolve()
    if args.video:
        video = Path(args.video).resolve()
        if not video.is_file():
            die(f"video not found: {video}")
        srt = next((p for p in (video.with_suffix(".SRT"), video.with_suffix(".srt")) if p.is_file()), None)
        if srt:
            import yaml

            cfg = yaml.safe_load((ROOT / "config" / "edge.footage.yaml").read_text())
            cfg["source"]["path"] = str(video)
            cfg["telemetry"]["path"] = str(srt)
            out = ROOT / "config" / f"{video.stem}.srt.sim.yaml"
            out.write_text(yaml.safe_dump(cfg, sort_keys=False))
            say(f"   {video.name} + {srt.name}: real flight log")
            say("   Check camera.width/height/dfov_deg in config/edge.footage.yaml match this drone.")
            return out
        say(f"   {video.name}: no .SRT flight log next to it, so the flight path is SIMULATED")
        run([sys.executable, "scripts/simulate_telemetry.py", video])
        return ROOT / "config" / f"{video.stem}.sim.yaml"
    sims = sorted((p for p in (ROOT / "config").glob("*.sim.yaml") if p != RUN_CONFIG),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if sims:
        return sims[0]
    die("no clip configured. Run with --video path/to/clip.mp4")


def ensure_model() -> str:
    if ENGINE.is_file():
        return "models/person-visdrone.engine"
    if WEIGHTS.is_file() or (ROOT / "best.pt").is_file():
        src = WEIGHTS if WEIGHTS.is_file() else ROOT / "best.pt"
        say(f"   No TensorRT engine yet: building it from {src.name} (about 8 minutes, once)...")
        run([sys.executable, "edge/export_model.py", "--model", src, "--output", ENGINE, "--imgsz", "640"])
        return "models/person-visdrone.engine"
    say("   Trained model not found: using the stock YOLOv8n engine.")
    return "models/yolov8n.engine"


def ensure_psycopg() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        say("   Installing the Postgres driver (once) to look up the laptop's address...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]>=3.2"])
        if r.returncode:
            return False
        import importlib

        importlib.invalidate_caches()
        return True


class CloudFinder(threading.Thread):
    """Keeps data/edge/cloud_url.txt pointing at the laptop's current address."""

    def __init__(self, fixed: str | None, database_url: str | None):
        super().__init__(daemon=True)
        self.fixed, self.database_url = fixed, database_url
        self.url: str | None = None

    def find(self) -> str | None:
        if self.fixed:
            return self.fixed.rstrip("/")
        if not self.database_url:
            return None
        try:
            found = lookup_cloud_url(self.database_url)
        except Exception as e:  # network, driver, auth
            say(f"   (could not read the address from Tiger Data: {str(e).splitlines()[0][:120]})")
            return None
        return found[0] if found else None

    def update(self) -> None:
        url = self.find()
        if url and url != self.url:
            self.url = url
            URL_FILE.parent.mkdir(parents=True, exist_ok=True)
            URL_FILE.write_text(url + "\n")
            say(f"[CLOUD] uploading to {url}")

    def run(self) -> None:
        while True:
            time.sleep(20)
            self.update()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", help="mp4 to process (uses its .SRT if present, else simulates)")
    ap.add_argument("--config", help="use this config as is")
    ap.add_argument("--mission-id", help="default: mission-<date>-<time>")
    ap.add_argument("--update", action="store_true", help="git pull before starting")
    ap.add_argument("--install-shortcut", action="store_true")
    args = ap.parse_args()
    os.chdir(ROOT)
    if args.install_shortcut:
        install_shortcut()
        return
    say("VANTAGE - drone side (Jetson)")

    if args.update:
        step("Updating the code")
        run(["git", "pull", "--ff-only"])

    step("1/5  Settings (.env)")
    load_env()
    require("INGEST_API_KEY", "so the cloud accepts this Jetson's uploads")
    database_url = os.environ.get("DATABASE_URL")
    fixed = os.environ.get("CLOUD_URL") if real(os.environ.get("CLOUD_URL")) else None
    say(f"   ingest key: set   cloud address: {'CLOUD_URL from .env' if fixed else 'looked up in Tiger Data' if real(database_url) else 'none (offline only)'}")

    step("2/5  Model, clip and website")
    import yaml

    base_cfg = choose_config(args)
    cfg = yaml.safe_load(base_cfg.read_text())
    cfg["mission_id"] = args.mission_id or time.strftime("mission-%Y%m%d-%H%M%S")
    det = cfg.setdefault("detector", {})
    if det.get("kind") == "yolo":          # real video: trained model + motion-compensated tracker
        det.update({"model_path": ensure_model(), "tracker": "botsort.yaml"})
    model = det.get("model_path", det.get("kind", "?"))
    up = cfg.setdefault("uploader", {})
    up.update({"enabled": True, "api_key_env": "INGEST_API_KEY",
               "server_url": up.get("server_url") or "http://127.0.0.1:8000",
               "server_url_file": str(URL_FILE.relative_to(ROOT)),
               "offline_flag_file": str(OFFLINE_FLAG.relative_to(ROOT))})
    RUN_CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    say(f"   clip config: {base_cfg.name}   model: {model}   tracker: {det.get('tracker', '-')}   mission: {cfg['mission_id']}")
    build_web_if_stale()

    step("3/5  Cloud address")
    URL_FILE.unlink(missing_ok=True)
    if not fixed and real(database_url) and not ensure_psycopg():
        database_url = None
    finder = CloudFinder(fixed, database_url if real(database_url) else None)
    finder.update()
    if finder.url:
        health = wait_http(f"{finder.url}/api/v1/health", 20)
        say(f"   cloud {'answers: ' + str(health) if health else 'does not answer yet: uploads will wait and retry'}")
    else:
        say("   No cloud address yet (laptop launcher not running?). The mission runs and saves on board;\n"
            "   uploads start by themselves once the laptop records its address.")
    finder.start()

    step("4/5  Starting the mission")
    if port_in_use(8080):
        die("port 8080 is in use: another edge pipeline is running. Close it, then run this again.")
    OFFLINE_FLAG.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "rescue_edge.main", "--config", str(RUN_CONFIG), "--serve"],
        cwd=ROOT / "edge", env={**os.environ, "PYTHONUNBUFFERED": "1"})

    if wait_http(f"{EDGE}/api/v1/status", 120):
        step("5/5  Running")
        say(f"""
   Drone dashboard: {EDGE}            (home page: offline demo buttons)
   Mission control: {EDGE}/#/dashboard
   Cloud copy:      {finder.url or '(waiting for the laptop)'}/#/dashboard
   Ctrl+C or close this window to stop.
""")
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", EDGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(EDGE)
    try:
        last = None
        while proc.poll() is None:
            time.sleep(10)
            try:
                st = http_json(f"{EDGE}/api/v1/status")
                u = st.get("uploader") or {}
                line = (f"[STATUS] frames {st.get('frames')}  on board v{u.get('local_version')}  "
                        f"cloud v{u.get('acked_version')}  "
                        f"{'NO LINK' if u.get('online') is False else 'link up'}"
                        f"{'  ATTENTION: ' + u['last_error'] if u.get('needs_attention') else ''}")
                if line != last:
                    say(line)
                    last = line
            except Exception:
                pass
        die("the pipeline stopped (see the output above).")
    except KeyboardInterrupt:
        say("\nStopping...")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
