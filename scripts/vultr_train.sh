#!/usr/bin/env bash
# One-shot training run on a fresh Ubuntu GPU machine (Vultr Cloud GPU), from the repo root:
#
#   tmux new -s train            # so a dropped SSH connection does not kill training
#   bash scripts/vultr_train.sh  # then Ctrl-b d to detach; `tmux attach -t train` to come back
#
# Steps (each is skipped if already done, so re-running resumes):
#   1. checks the GPU, installs system packages + a Python venv with Ultralytics
#   2. downloads VisDrone DET train + val (~1.6 GB) and builds the tiled person dataset
#   3. fine-tunes YOLOv8n (EPOCHS, default 50)
#   4. evaluates stock vs fine-tuned YOLOv8n on VisDrone val (tiles 640 and 320)
#   5. prints the scp command to copy best.pt to your laptop
#
# Settings (environment variables): EPOCHS=50  BATCH=-1 (auto)  ULTRALYTICS=8.3.253
#   VENV=~/rescue-venv  SMOKE=1 (CPU, 20 images, 1 epoch: tests the script, not a real run)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
EPOCHS="${EPOCHS:-50}"
BATCH="${BATCH:--1}"
ULTRALYTICS="${ULTRALYTICS:-8.3.253}"
VENV="${VENV:-$HOME/rescue-venv}"
SMOKE="${SMOKE:-0}"
DS="$REPO/datasets"
NAME="person-yolov8n"
RELEASES="https://github.com/ultralytics/assets/releases/download"
LOG="$REPO/runs/rescue/vultr_train.log"
mkdir -p "$DS" "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
say() { printf '\n=== %s  [%s]\n' "$*" "$(date -u +%H:%M:%SZ)"; }

DEVICE=0
LIMIT=()
if [[ "$SMOKE" == 1 ]]; then
  DEVICE=cpu; EPOCHS=1; BATCH=8; NAME="smoke"; LIMIT=(--limit 20)
  say "SMOKE MODE: CPU, 20 images, 1 epoch. This only tests the script."
fi

say "1/5 GPU and packages"
if [[ "$DEVICE" != cpu ]]; then
  if ! nvidia-smi; then
    echo "nvidia-smi failed: no NVIDIA driver. Redeploy with a GPU-enabled image (see docs/training.md)." >&2
    exit 1
  fi
fi
need=()
for pkg in python3-venv libgl1 libglib2.0-0 unzip; do
  # Ubuntu 24.04 renamed some libraries with a t64 suffix (libglib2.0-0 -> libglib2.0-0t64)
  dpkg -s "$pkg" >/dev/null 2>&1 || dpkg -s "${pkg}t64" >/dev/null 2>&1 || need+=("$pkg")
done
if ((${#need[@]})); then
  echo "installing: ${need[*]}"
  if [[ $EUID -eq 0 ]]; then SUDO=(); else SUDO=(sudo); fi
  "${SUDO[@]}" apt-get update -qq
  DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" apt-get install -y -qq "${need[@]}"
fi
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
PY="$VENV/bin/python"
if ! "$PY" -c "import ultralytics, sys; sys.exit(ultralytics.__version__ != '$ULTRALYTICS')" 2>/dev/null; then
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q "ultralytics==$ULTRALYTICS" pyyaml
fi
"$PY" - "$DEVICE" <<'EOF'
import sys, torch, ultralytics
print(f"ultralytics {ultralytics.__version__}, torch {torch.__version__}, CUDA {torch.cuda.is_available()}")
if sys.argv[1] != "cpu":
    if not torch.cuda.is_available():
        sys.exit("PyTorch cannot see the GPU (CPU-only build or driver problem); see docs/training.md")
    p = torch.cuda.get_device_properties(0)
    print(f"GPU: {p.name}, {p.total_memory / 2**30:.0f} GB")
EOF

say "2/5 VisDrone data"
for split in train val; do
  [[ "$SMOKE" == 1 && "$split" == train ]] && continue   # smoke: val only, used for both
  if [[ ! -d "$DS/VisDrone2019-DET-$split/images" ]]; then
    curl -fL --retry 3 -o "$DS/VisDrone2019-DET-$split.zip" "$RELEASES/v0.0.0/VisDrone2019-DET-$split.zip"
    unzip -q -o "$DS/VisDrone2019-DET-$split.zip" -d "$DS"
    rm -f "$DS/VisDrone2019-DET-$split.zip"
  fi
done
TRAIN_SRC="$DS/VisDrone2019-DET-train"
[[ "$SMOKE" == 1 ]] && TRAIN_SRC="$DS/VisDrone2019-DET-val"
PERSON="$DS/visdrone-person"
[[ "$SMOKE" == 1 ]] && PERSON="$DS/visdrone-person-smoke"
if [[ ! -f "$PERSON/data.yaml" ]]; then
  rm -rf "$PERSON"
  "$PY" scripts/prepare_visdrone.py --train "$TRAIN_SRC" --val "$DS/VisDrone2019-DET-val" \
      --out "$PERSON" "${LIMIT[@]}"
fi

say "3/5 training ($EPOCHS epochs)"
RUN="$REPO/runs/rescue/$NAME"
BEST="$RUN/weights/best.pt"
[[ -f yolov8n.pt ]] || curl -fL --retry 3 -o yolov8n.pt "$RELEASES/v8.3.0/yolov8n.pt"
if [[ -f "$RUN/.done" ]]; then
  echo "already trained: $BEST"
elif [[ -f "$RUN/weights/last.pt" ]]; then
  echo "resuming the interrupted run from $RUN/weights/last.pt"
  "$PY" - "$RUN/weights/last.pt" <<'EOF'
import sys
from ultralytics import YOLO
try:
    YOLO(sys.argv[1]).train(resume=True)
except AssertionError as e:  # finished just before the connection dropped
    if "nothing to resume" not in str(e):
        raise
    print("training had already finished; using its weights")
EOF
  touch "$RUN/.done"
else
  rm -rf "$RUN"   # a run that died before saving any weights: start clean, same name
  WORKERS=$(( $(nproc) < 8 ? $(nproc) : 8 ))
  "$PY" scripts/train_detector.py --data "$PERSON/data.yaml" --model yolov8n.pt \
      --epochs "$EPOCHS" --batch "$BATCH" --device "$DEVICE" --workers "$WORKERS" --name "$NAME"
  touch "$RUN/.done"
fi

say "4/5 evaluation on VisDrone val: stock vs fine-tuned"
EVAL_LIMIT=()
[[ "$SMOKE" == 1 ]] && EVAL_LIMIT=(--limit 3)
RESULTS="$RUN/eval.md"
: > "$RESULTS"
for weights in yolov8n.pt "$BEST"; do
  label="stock YOLOv8n (COCO)"; [[ "$weights" == "$BEST" ]] && label="fine-tuned ($NAME)"
  for tile in 640 320; do
    echo "## $label, tile $tile" | tee -a "$RESULTS"
    "$PY" scripts/eval_detector.py --images "$DS/VisDrone2019-DET-val/images" \
        --labels "$DS/VisDrone2019-DET-val/annotations" --format visdrone --weights "$weights" \
        --imgsz 640 --conf 0.1 0.25 --tile "$tile" --device "$DEVICE" "${EVAL_LIMIT[@]}" 2>/dev/null \
        | grep -E '^\|' | tee -a "$RESULTS"
  done
done

say "5/5 done"
IP=$(hostname -I | awk '{print $1}')   # Vultr puts the public IP on the instance itself
cat <<EOF
Trained weights : $BEST ($(du -h "$BEST" | cut -f1))
Results table   : $RESULTS
Full log        : $LOG

On YOUR LAPTOP, copy the weights (and the results) back:
  scp root@$IP:$BEST person-visdrone.pt
  scp root@$IP:$RESULTS eval.md

Then DESTROY this instance in the Vultr console (a stopped instance is still billed).
EOF
