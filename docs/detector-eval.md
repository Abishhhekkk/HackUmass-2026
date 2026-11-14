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

Next steps, in the order the project brief sets (YOLO stays):
1. Pick `imgsz` / `conf_threshold` from this table and the Jetson's measured FPS; rebuild
   the TensorRT engine at that size (`detector.imgsz` must equal the export size).
2. Fly lower in the footage; evaluate on your own labelled frames (`--format yolo`).
3. Tiling (split large frames into 640 crops) if 1280 is too slow on the Jetson.
4. VisDrone-trained YOLO weights with `person_classes: [0, 1]`.
