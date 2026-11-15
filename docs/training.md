# Training an aerial person detector (GPU machine) and loading it on the Jetson

Stock YOLOv8n was trained on COCO (mostly ground-level photos) and misses most people seen
from a drone (`docs/detector-eval.md`). Fine-tuning it on aerial imagery (VisDrone) fixes
the *model* side; tiling already fixes the *pixels* side. Training needs a real GPU, so it
runs **off the Jetson**: on a laptop with an NVIDIA GPU, or a Vultr cloud GPU. The only
thing that moves to the Jetson is `best.pt` (~6 MB). The Jetson stays offline.

```
GPU machine (laptop or Vultr)                          Jetson Orin Nano
VisDrone ─ prepare_visdrone.py ─ train_detector.py ─▶ best.pt ──copy──▶ export_model.py ─▶ .engine
                                   eval_detector.py (compare with stock)        (built ON the Jetson)
```

## What you need

* An **NVIDIA** GPU with a CUDA build of PyTorch. 8 GB of VRAM or more is comfortable for
  YOLOv8n at 640 (the script picks the batch size). Apple/AMD GPUs are not covered here.
* ~10 GB of disk (1.6 GB download + ~22k tiles).
* The **same Ultralytics version** on the GPU machine and the Jetson (check with
  `python -c "import ultralytics; print(ultralytics.__version__)"`; this repo was tested
  with 8.3.253). A `.pt` from a newer major version may not load on an older one.

Time is a rough guess, **not measured here** (this repo was only smoke-tested on a CPU):
~22k tiles per epoch; 50 epochs is on the order of 1–3 hours on a data-centre GPU and
several hours on a laptop GPU. Early stopping ends sooner if it stops improving.

## Option A: Vultr cloud GPU (one script)

1. **SSH key** (once, on your laptop): `ssh-keygen -t ed25519` (works in Windows PowerShell
   too), then add the contents of `~/.ssh/id_ed25519.pub` in the Vultr console under your
   account's SSH Keys.
2. **Deploy** a *Cloud GPU* instance: an NVIDIA plan with at least 8 GB of GPU memory (more
   vCPUs speed up data loading), an **Ubuntu image marked GPU-enabled / with NVIDIA drivers**
   (Vultr's docs say these ship with the driver and CUDA), your SSH key. Note the IP.
3. **Connect and start** (the repo is public, so no GitHub login is needed):
   ```bash
   ssh root@<ip>
   git clone https://github.com/Abishhhekkk/HackUmass-2026.git && cd HackUmass-2026
   git checkout claude          # until merged
   tmux new -s train
   bash scripts/vultr_train.sh  # EPOCHS=50 by default; e.g. EPOCHS=30 bash scripts/vultr_train.sh
   ```
   `Ctrl-b` then `d` detaches (training continues if your laptop sleeps or SSH drops);
   `tmux attach -t train` reattaches. Re-running the script resumes where it stopped.
4. **When it prints "5/5 done"**, run the two `scp` lines it prints **on your laptop**, look at
   `eval.md` (stock vs fine-tuned recall), then **destroy the instance** in the console. A
   stopped instance is still billed; destroying it ends billing.

`scripts/vultr_train.sh` checks the GPU, installs Ultralytics 8.3.253 in a venv, downloads
VisDrone (~1.6 GB), builds the tiled dataset, trains, evaluates stock vs fine-tuned on VisDrone
val at tiles 640 and 320, and logs everything to `runs/rescue/vultr_train.log`. The manual
steps below are what it runs.

## Option B: your laptop

Use any laptop/desktop with an NVIDIA GPU. Install the CUDA build of PyTorch from
pytorch.org (the plain `pip install torch` on Windows is CPU-only), then follow the steps
below. On Windows PowerShell, write each command on one line (drop the `\`).

## Steps on the GPU machine

```bash
git clone https://github.com/Abishhhekkk/HackUmass-2026.git && cd HackUmass-2026
git checkout claude          # until merged
python -m pip install "ultralytics==8.3.253" opencv-python pyyaml
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"  # must be True

# 1. VisDrone DET (Ultralytics' mirror). Non-commercial research licence: check before other use.
mkdir -p datasets && cd datasets
curl -LO https://github.com/ultralytics/assets/releases/download/v0.0.0/VisDrone2019-DET-train.zip
curl -LO https://github.com/ultralytics/assets/releases/download/v0.0.0/VisDrone2019-DET-val.zip
unzip -q VisDrone2019-DET-train.zip && unzip -q VisDrone2019-DET-val.zip && cd ..

# 2. Tiles + single-class labels (person = pedestrian + people)
python scripts/prepare_visdrone.py --train datasets/VisDrone2019-DET-train \
    --val datasets/VisDrone2019-DET-val --out datasets/visdrone-person

# 3. Fine-tune from COCO-pretrained YOLOv8n at the Jetson's 640 engine size
python scripts/train_detector.py --data datasets/visdrone-person/data.yaml --epochs 50

# 4. Compare with the stock model on the untouched VisDrone val images (full frames)
W=runs/rescue/person-yolov8n/weights/best.pt
for T in 320 640; do
  python scripts/eval_detector.py --images datasets/VisDrone2019-DET-val/images \
    --labels datasets/VisDrone2019-DET-val/annotations --format visdrone \
    --weights $W --imgsz 640 --conf 0.1 0.25 --tile $T --device 0
done
```

The tiles are cut at native resolution, so the model learns people at the size it sees
them with `tile: 640`. Step 4 tells you whether `tile: 640` (9 engine runs per 1080p frame)
is now enough or `tile: 320` (33 runs) is still worth its cost. Put the numbers in
`docs/detector-eval.md`. Step 4 evaluates on VisDrone val; step 2 also tiles val for
training-time validation, but no val image is used to *train*.

## On the Jetson

```bash
cp best.pt models/person-visdrone.pt          # e.g. from a USB stick, or scp from your laptop
python3 edge/export_model.py --model models/person-visdrone.pt \
    --output models/person-visdrone.engine --imgsz 640
```

Then in `config/edge.jetson.yaml` (or your footage config): set
`detector.model_path: models/person-visdrone.engine`, keep `imgsz: 640`, and set `tile` from
step 4. Nothing else changes: the model's only class is 0 = person, as for COCO. Check it
with the opt-in pipeline test and note the FPS:

```bash
RESCUE_YOLO_WEIGHTS=models/person-visdrone.engine RESCUE_YOLO_DEVICE=0 RESCUE_YOLO_TILE=640 \
    python3 -m pytest -k real_yolo -s
```

(That test uses a ground-level photo, so it checks the pipeline, not aerial accuracy; a
VisDrone-trained model may find fewer people in it than COCO weights do.)

## Training on the Jetson itself

Possible (the Orin Nano's GPU runs PyTorch training), but slow: expect many hours for 50
epochs, and the board cannot run the demo meanwhile. Use `--batch 8 --workers 2` to fit
its shared 8 GB of memory. A laptop or Vultr GPU is the better choice.

## Model weights and Git

Do not commit `*.pt`, `*.engine` or datasets (`.gitignore` already excludes weights and
engines; keep `datasets/` and `runs/` out too).
