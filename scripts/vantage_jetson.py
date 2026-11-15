#!/usr/bin/env python3
"""Vantage Jetson launcher: the drone side in one go (started by Vantage-Jetson.sh).

1. Cloud address (the laptop launcher prints it) and ingest key: asked once, remembered in
   ~/.config/vantage/ (the key file is readable only by you). Both are checked against the cloud
   before the mission starts; Enter keeps the last values.
2. Footage: every .mp4 in data/footage/. A clip with its DJI .SRT uses the real flight log; a
   clip without one gets a SIMULATED flight path (scripts/simulate_telemetry.py) on first use.
3. Model: the fine-tuned TensorRT engine if present (built from its .pt on first use, ~8 min).
4. Builds the website when its source changed, clears the demo "offline" switch, starts the
   pipeline with the local dashboard, opens it, and prints progress and upload status.

    ./Vantage-Jetson.sh            normal start
    ./Vantage-Jetson.sh --setup    re-enter the cloud address and key
    ./Vantage-Jetson.sh --install  add a "Vantage" icon to the desktop and app menu

Ctrl+C stops the mission; snapshots stay on the Jetson.
"""
from __future__ import annotations

import argparse
import getpass
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
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONF_DIR = Path.home() / ".config" / "vantage"
SETTINGS = CONF_DIR / "jetson.json"
KEY_FILE = CONF_DIR / "ingest_key"
RUN_CONFIG = ROOT / "config" / ".vantage-run.sim.yaml"   # *.sim.yaml is git-ignored
FOOTAGE = ROOT / "data" / "footage"
EDGE = "http://127.0.0.1:8080"
NOISE = ("UserWarning: Found GPU0", "The following list shows", "which supports hardware CC",
         "No published PyTorch CUDA builds", "_warn_unsupported_code", "[TRT] [I]")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n== {msg}")


def die(msg: str) -> None:
    say(f"\nSTOPPED: {msg}")
    sys.exit(1)


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{shown}: ").strip()
    except EOFError:
        value = ""
    return value or default


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def save_settings(s: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2))


def http(method: str, url: str, headers: dict | None = None, data: bytes | None = None, timeout: float = 8):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ------------------------------------------------------------------ 1. cloud
def check_cloud(url: str, key: str) -> str | None:
    """None if the cloud answers and accepts the key, else the problem."""
    try:
        code, body = http("GET", f"{url}/api/v1/health")
    except Exception as e:
        return f"cannot reach {url} ({e.__class__.__name__}). Is the laptop launcher running? The address changes on every restart."
    if code != 200:
        return f"{url} answered {code}: not the Vantage cloud API"
    if json.loads(body).get("db") != "postgres":
        return "the cloud API is not using Tiger Data (db = sqlite): fix DATABASE_URL in the laptop's .env"
    # An empty upload: 401 = wrong key, 422 = key accepted (nothing is stored).
    code, _ = http("POST", f"{url}/api/v1/snapshots", headers={"X-API-Key": key}, data=b"")
    if code == 401:
        return "the cloud rejected the ingest key: it must equal INGEST_API_KEY in the laptop's .env"
    return None


def setup_cloud(settings: dict, force: bool) -> tuple[str | None, str | None]:
    step("Cloud connection")
    key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
    url = settings.get("cloud_url", "")
    while True:
        entered = ask("Cloud address from the laptop launcher (Enter = keep, 'none' = no cloud)",
                      "" if force else url)
        if entered.lower() == "none":
            say("   no cloud: snapshots stay on the Jetson only")
            return None, None
        url = entered.rstrip("/.").strip()
        if not re.match(r"^https?://[^\s/]+$", url):
            say("   that does not look like https://something.trycloudflare.com; try again")
            continue
        if force or not key:
            key = getpass.getpass("Ingest key (same as INGEST_API_KEY in the laptop's .env; typing is hidden): ").strip()
        problem = check_cloud(url, key)
        if problem is None:
            break
        say(f"   {problem}")
        if "ingest key" in problem:
            force = True  # ask for the key again
        if ask("Try again? (y = retry, n = run without the cloud)", "y").lower().startswith("n"):
            return None, None
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key)
    KEY_FILE.chmod(0o600)
    settings["cloud_url"] = url
    save_settings(settings)
    say(f"   cloud OK: {url} answers, stores in Tiger Data, and accepts the key")
    return url, key


# ------------------------------------------------------------------ 2. footage
def video_size(path: Path) -> tuple[int, int]:
    import cv2

    cap = cv2.VideoCapture(str(path))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


def clips() -> list[dict]:
    found = []
    for mp4 in sorted(p for p in FOOTAGE.glob("*") if p.suffix.lower() == ".mp4"):
        if mp4.stem.endswith(("_annotated", "_stock")):
            continue
        srt = next((p for p in (mp4.with_suffix(".SRT"), mp4.with_suffix(".srt")) if p.exists()), None)
        sim = ROOT / "config" / f"{mp4.stem}.sim.yaml"
        kind = "srt" if srt else ("sim" if sim.exists() else "sim-new")
        found.append({"video": mp4, "srt": srt, "sim": sim, "kind": kind})
    return found


def choose_clip(settings: dict) -> dict:
    step("Footage")
    options = clips()
    if not options:
        die(f"no .mp4 in {FOOTAGE}. Copy the drone video there (with its .SRT if it has one) and start again.")
    labels = {"srt": "real flight log (.SRT)", "sim": "simulated flight path", "sim-new": "simulated flight path (set up now)"}
    last = settings.get("clip")
    default = next((str(i + 1) for i, o in enumerate(options) if o["video"].name == last), "1")
    for i, o in enumerate(options, 1):
        say(f"   {i}. {o['video'].name:<32} {labels[o['kind']]}")
    while True:
        pick = ask("Which clip", default)
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            chosen = options[int(pick) - 1]
            settings["clip"] = chosen["video"].name
            return chosen
        say("   type one of the numbers above")


def base_config(clip: dict) -> dict:
    if clip["kind"] == "srt":
        cfg = yaml.safe_load((ROOT / "config" / "edge.footage.yaml").read_text())
        w, h = video_size(clip["video"])
        cfg["source"]["path"] = str(clip["video"].relative_to(ROOT))
        cfg["telemetry"]["kind"] = "srt"
        cfg["telemetry"]["path"] = str(clip["srt"].relative_to(ROOT))
        cfg["camera"]["width"], cfg["camera"]["height"] = w, h
        return cfg
    if clip["kind"] == "sim-new":
        say("   no flight log: creating a SIMULATED one (positions are a demo, not real GPS)")
        if subprocess.call([sys.executable, "scripts/simulate_telemetry.py", str(clip["video"])], cwd=ROOT):
            die("simulate_telemetry.py failed (see above).")
    return yaml.safe_load(clip["sim"].read_text())


# ------------------------------------------------------------------ 3. model
def ensure_engine(cfg: dict) -> None:
    det = cfg.setdefault("detector", {})
    preferred = ROOT / "models" / "person-visdrone.engine"
    if preferred.exists() or (ROOT / "models" / "person-visdrone.pt").exists():
        det["model_path"] = "models/person-visdrone.engine"
    det["tracker"] = "botsort.yaml"   # camera-motion compensation (docs/detector-eval.md)
    path = ROOT / det.get("model_path", "models/yolov8n.engine")
    if path.suffix != ".engine" or path.exists():
        say(f"   model: {path.relative_to(ROOT)}  tracker: botsort")
        return
    pt = path.with_suffix(".pt")
    if not pt.exists():
        die(f"{path.relative_to(ROOT)} and {pt.relative_to(ROOT)} are both missing. Copy the trained model "
            "to models/person-visdrone.pt (docs/training.md).")
    step(f"Building the TensorRT engine from {pt.name} (first time only, about 8 minutes)")
    if subprocess.call([sys.executable, "edge/export_model.py", "--model", str(pt), "--output", str(path),
                        "--imgsz", str(det.get("imgsz", 640))], cwd=ROOT):
        die("engine export failed (see above).")


# ------------------------------------------------------------------ 4. website
def ensure_website() -> None:
    web = ROOT / "web"
    built = web / "dist" / "index.html"
    newest = 0.0
    for p in [web / "src", web / "public", web / "index.html", web / "package.json", web / "package-lock.json"]:
        for f in ([p] if p.is_file() else p.rglob("*") if p.exists() else []):
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
    if built.exists() and built.stat().st_mtime >= newest:
        return
    step("Building the website (its source changed)")
    npm = shutil.which("npm")
    if not npm:
        say("   npm not found (sudo apt install -y nodejs npm): " +
            ("using the existing, older build" if built.exists() else "the dashboard will be missing"))
        return
    if subprocess.call([npm, "install", "--no-audit", "--no-fund"], cwd=web) or \
            subprocess.call([npm, "run", "build"], cwd=web):
        say("   website build failed; continuing with the previous build")


# ------------------------------------------------------------------ 5. run
def install_icon() -> None:
    launcher = ROOT / "Vantage-Jetson.sh"
    entry = (f"[Desktop Entry]\nType=Application\nName=Vantage (Jetson)\n"
             f"Comment=Start the Vantage drone pipeline\nExec={launcher}\nPath={ROOT}\n"
             f"Icon={ROOT / 'web' / 'public' / 'favicon.svg'}\nTerminal=true\nCategories=Utility;\n")
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    targets = [apps / "vantage-jetson.desktop"]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        targets.append(desktop / "vantage-jetson.desktop")
    for t in targets:
        t.write_text(entry)
        t.chmod(0o755)
        subprocess.call(["gio", "set", str(t), "metadata::trusted", "true"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    say("Added 'Vantage (Jetson)' to the app menu" + (" and the desktop" if len(targets) > 1 else "") +
        ". On the desktop icon, right-click > Allow Launching the first time.")


def stream(proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        if not any(n in line for n in NOISE) and line.strip():
            print(f"   {line.rstrip()}", flush=True)


def run_mission(cfg: dict, mission: str, key: str | None) -> None:
    RUN_CONFIG.write_text("# written by scripts/vantage_jetson.py for the current run\n" + yaml.safe_dump(cfg, sort_keys=False))
    (ROOT / cfg["uploader"].get("offline_flag_file", "data/edge/OFFLINE")).unlink(missing_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if key:
        env["INGEST_API_KEY"] = key
    step(f"Mission {mission}: starting (Ctrl+C to stop)")
    proc = subprocess.Popen([sys.executable, "-m", "rescue_edge.main", "--config", str(RUN_CONFIG), "--serve",
                             "--mission-id", mission], cwd=ROOT / "edge", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=stream, args=(proc,), daemon=True).start()
    opened = False
    last_line = ""
    done_said = False
    try:
        while proc.poll() is None:
            time.sleep(5)
            try:
                code, body = http("GET", f"{EDGE}/api/v1/status", timeout=3)
                st = json.loads(body) if code == 200 else None
            except Exception:
                st = None
            if not st:
                continue
            if not opened:
                if shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", EDGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                say(f"   dashboard: {EDGE}  (on this Jetson)")
                opened = True
            u = st.get("uploader") or {}
            link = "no cloud" if not u else ("LINK CUT (demo)" if u.get("online") is False and
                                            (ROOT / "data/edge/OFFLINE").exists() else
                                            "offline, retrying" if u.get("online") is False else
                                            "sending" if u.get("pending") else "cloud in sync")
            line = (f"   [{time.strftime('%H:%M:%S')}] frames {st.get('frames')}  detections {st.get('detections')}  "
                    f"on board v{u.get('local_version', '-')}  cloud v{u.get('acked_version', '-')}  {link}")
            if u.get("needs_attention"):
                line += f"  !! {u.get('last_error')}"
            if line.split("] ", 1)[1] != last_line:
                say(line)
                last_line = line.split("] ", 1)[1]
            if st.get("finished") and not done_said and (not u or not u.get("pending")):
                say("\n   Mission finished" + (" and the cloud has the final snapshot." if u else ".") +
                    " The dashboard stays up; Ctrl+C to close it.")
                done_said = True
    except KeyboardInterrupt:
        say("\nStopping... (snapshots stay in data/edge/snapshots)")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true", help="re-enter the cloud address and ingest key")
    ap.add_argument("--install", action="store_true", help="add a desktop / app-menu icon and exit")
    args = ap.parse_args()
    os.chdir(ROOT)
    if args.install:
        install_icon()
        return
    say("Vantage Jetson (drone side)")
    settings = load_settings()
    url, key = setup_cloud(settings, args.setup)
    clip = choose_clip(settings)
    cfg = base_config(clip)
    step("Model")
    ensure_engine(cfg)
    up = cfg.setdefault("uploader", {})
    up["enabled"] = bool(url)
    if url:
        up["server_url"] = url
    up.setdefault("api_key_env", "INGEST_API_KEY")
    up.setdefault("offline_flag_file", "data/edge/OFFLINE")
    ensure_website()
    mission = ask("\nMission name", f"mission-{time.strftime('%m%d-%H%M')}")
    mission = re.sub(r"[^A-Za-z0-9._-]", "-", mission).strip(".-") or f"mission-{time.strftime('%m%d-%H%M')}"
    save_settings(settings)
    run_mission(cfg, mission, key)


if __name__ == "__main__":
    main()
