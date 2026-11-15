# Detector evaluation (person recall on aerial imagery)

`scripts/eval_detector.py` measures how many labelled people the detector finds (recall, the
priority metric), how many of its boxes are right (precision), and recall on **small**
people (< 32 × 32 px, most aerial people), across image sizes and confidence thresholds.
Person-only, IoU ≥ 0.5, greedy matching; logic in `edge/rescue_edge/detector_eval.py`.

```bash
python scripts/eval_detector.py --images <dir> --labels <dir> --format visdrone|yolo|voc \
    --weights yolov8n.pt --imgsz 640 960 1280 --conf 0.1 0.25 0.35 [--device 0] [--csv out.csv]
```

| Dataset | `--format` | Notes |
|---|---|---|
| VisDrone2019-DET val | `visdrone` | pedestrian (1) + people (2) merged; category 0 "ignored regions" respected |
| SARD, own labelled footage | `yolo` | `class cx cy w h` normalised; `--label-classes` if person is not class 0 |
| HERIDAL | `voc` | Pascal VOC XML; names human/person/pedestrian/people |

`--model-classes` = the detector's person class IDs: `0` for stock COCO YOLO, e.g. `0 1` for
VisDrone-trained weights. Speeds printed are for the machine the script runs on.

## Result: stock YOLOv8n (COCO), VisDrone2019-DET val

548 images, 13,969 labelled people. Run 2026-11-14 on a cloud **CPU** (Ultralytics 8.3.253,
`yolov8n.pt` from the Ultralytics release). Accuracy carries over to the Jetson; the ms/img
column does not (measure FPS on the Jetson with the TensorRT engine).

| imgsz | conf | recall | precision | recall (small) | TP | FP | FN | CPU ms/img |
|---|---|---|---|---|---|---|---|---|
| 1280 | 0.10 | **33.4%** | 54.9% | 28.3% | 4668 | 3838 | 9301 | 120 |
| 960 | 0.10 | 24.6% | 55.9% | 19.2% | 3437 | 2713 | 10532 | 69 |
| 1280 | 0.25 | 21.6% | 79.4% | 16.4% | 3015 | 783 | 10954 | 120 |
| 1280 | 0.35 | 16.6% | 87.3% | 11.8% | 2312 | 337 | 11657 | 120 |
| 960 | 0.25 | 14.8% | 78.3% | 10.0% | 2068 | 573 | 11901 | 69 |
| 640 | 0.10 | 13.9% | 54.3% | 9.2% | 1947 | 1637 | 12022 | 34 |
| 960 | 0.35 | 10.8% | 86.0% | 6.5% | 1506 | 245 | 12463 | 69 |
| 640 | 0.25 | 7.4% | 74.5% | 3.8% | 1039 | 355 | 12930 | 34 |
| **640** | **0.35** | **5.0%** | 83.1% | 2.2% | 702 | 143 | 13267 | 34 |

**What this means.** The current Jetson config (`imgsz: 640`, `conf_threshold: 0.35`, bold
row) finds about **1 in 20** labelled people in VisDrone. VisDrone is a hard case (high
altitude, crowds, tiny people), so a low-altitude flight over a field will do better, but
the trend is the point: a larger input size and a lower threshold raise recall several-fold
(1280 / 0.10 finds ~6.7× as many), at the cost of more false alarms (which the tracker's
`min_frames` and human review absorb) and ~4× the compute per frame.

## Why: is it the model?

Mostly it is **pixels**, then the training data. In VisDrone val the median labelled person
is 25 px tall and 12 px wide. Squeezing a ~1360 px frame into 640 px leaves the median
person 12 px tall (about 6 px wide); 68% end up under 16 px. YOLO's finest grid is 8 px
per cell, so they are one or two cells. Even at native resolution (1280), stock COCO
weights find only a third: COCO is mostly ground-level photos, and people seen from above
look different.

For your own footage (1920 px frame, 70° lens, nadir), a standing person's shoulders:

| Altitude | cm/px | shoulders, native | after resize to 640 |
|---|---|---|---|
| 15 m | 1.1 | 41 px | 14 px |
| 20 m | 1.5 | 31 px | 10 px |
| 30 m | 2.2 | 21 px | 7 px |
| 50 m | 3.6 | 12 px | 4 px |

## Experiments (same 548 images, IoU 0.5 unless noted, conf 0.10)

| Change | Recall | Precision | CPU ms/img | What it shows |
|---|---|---|---|---|
| Old config: v8n, 640 full frame, conf 0.35 | 5.0% | 83.1% | 34 | baseline |
| v8n, 640 full frame, conf 0.10 | 13.9% | 54.3% | 34 | threshold alone: ~3× |
| v8n, 1280 full frame | 33.4% | 54.9% | 120 | native resolution |
| v8n, 1280, scored at IoU 0.3 | 37.4% | 61.5% | 120 | misses are real, not loose boxes |
| v8n, 1920 full frame (upscaled) | 43.8% | 51.9% | 238 | more pixels per person keeps helping |
| v8s, 1280 | 40.1% | 61.2% | 268 | bigger model: +7 pts for 2.3× compute |
| v8m, 1280 | 41.7% | 71.2% | 638 | +8 pts for 5.5× compute: diminishing |
| v8n, **tiles 640 px** + full frame, 640 engine | 35.2% | 59.8% | 377 | = 1280 full frame, but fixed 640 engine |
| v8n, **tiles 320 px** (2× up) + full frame, 640 engine | **47.5%** | 61.2% | 896 | **best**: ~9.5× the old setting; now the Jetson config |

Tiling = `scripts/eval_detector.py --tile <px>` / `edge/rescue_edge/tiling.py`: overlapping
crops (20%) through the same fixed-size model, boxes shifted back, boxes cut by an inner
tile edge dropped (the neighbour tile or the full-frame pass sees that person whole; without
this a person larger than a tile came back as several fragments: the tiles-320 row was 56.2%
precision before, 61.2% after, same recall), duplicates merged. 320 px tiles on a 1360×765 frame = 18 tiles + 1 full pass.
CPU times are wall clock on a cloud CPU; on the Jetson multiply-out from the per-640-run
TensorRT time you measure.

## Speed on the Jetson (measured)

Orin Nano Developer Kit Super, JetPack 7.2.1, MAXN power mode, YOLOv8n TensorRT FP16 engine
(640×640), one 1920×1080 frame, `scripts/bench_detector.py`, 2026-11-14:

| Mode | Engine runs / frame | ms / frame | frames / s |
|---|---|---|---|
| whole frame | 1 | 18 | 55 |
| tiles 640 px | 9 | 114 | 8.8 |
| tiles 480 px | 16 | 227 | 4.4 |
| tiles 320 px | 33 | 454 | 2.2 |

About 14 ms per engine run. The configs use **tile 640** (keeps up with the scheduler: every 4th
frame of a 30 fps video while searching, every 3rd while people are in view) and
`conf_threshold: 0.10`. 320 px tiles find more people with the stock COCO weights but are too
slow; re-check with the VisDrone-trained model (`docs/training.md`), which sees people at
native resolution, so 640 px tiles should suit it.

Next steps (YOLO stays, per the project brief):
1. **Measure FPS on the Jetson** with the TensorRT engine (`docs/hardware.md`, step 5). If 33
   engine runs per 1080p frame is too slow, use `tile: 480` (16 runs) or `640` (9 runs).
2. **Aerial-trained weights**: fine-tune YOLOv8n on VisDrone train. The Orin Nano's GPU can
   train, slowly (expect hours); re-export the engine afterwards. Untested here.
3. A bigger COCO model (v8s/v8m) only if the Jetson has time to spare: small gain per cost.

## Is the people COUNT right? (`scripts/eval_counting.py`)

Detection recall/precision is scored per image; it cannot catch one person becoming several
tracks. `scripts/eval_counting.py` runs the pipeline's detector + tiling + ByteTrack on aerial
video with per-person IDs (VisDrone-MOT format) and reports, per sequence: true people,
counted people (tracked in >= 3 evaluated frames), found, false tracks, duplicate tracks
and the count error. Scoring is in image space; the pipeline's ground-position merge
(`merge_radius_m`) can only lower its count further.

### Tracker: camera-motion compensation (measured)

At a realistic speed (~8 m/s at 50 m, 1080p, processing every 3rd frame) a person 8-10 px wide
moves ~20 px between processed frames, so ByteTrack's box-overlap matching loses them and new
tracks are never confirmed. BoT-SORT (`tracker: botsort.yaml`, Ultralytics' ByteTrack plus
sparse-optical-flow camera-motion compensation, ReID off) fixes this.

Fine-tuned model, 640 px tiles, conf 0.10, every 3rd frame, `eval_counting.py` on 3 panned
VisDrone-DET val images (`scripts/make_pan_sequences.py`, 492 labelled people, 24 frames each;
CPU run in development, not on the Jetson):

| tracker | counted | real people found | false tracks | duplicates |
|---|---|---|---|---|
| ByteTrack | 103 | 6 (1%) | 97 | 0 |
| BoT-SORT (camera-motion compensation) | 406 | 305 (62%) | 100 | 1 |

On the Jetson (Orin Nano Super, TensorRT FP16 fine-tuned engine), 20 panned sequences, 2,292
labelled people, 60 frames each, every 3rd frame evaluated:

| tracker | counted | real people found | false tracks | duplicates | time, 400 frames |
|---|---|---|---|---|---|
| ByteTrack | 1,329 | 456 (20%) | 842 | 31 | 78 s |
| BoT-SORT | 2,194 | 1,551 (68%) | 595 | 48 | 102 s |

BoT-SORT adds ~60 ms per processed frame here (the times include reading images from disk).
In the full pipeline on the 1,883-frame (~63 s) simulated clip it still kept real time: every
scheduled frame was processed (142 search + 439 active) and the run took ~63 s.

Limits: panning a still image has no moving people, occlusion or blur. The 68% is "found at
least once"; the near-correct total (2,194 vs 2,292) is partly misses and false tracks
cancelling out.
