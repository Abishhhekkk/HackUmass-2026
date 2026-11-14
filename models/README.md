# Models (not committed)

- `yolov8n.pt` downloads automatically the first time Ultralytics runs (AGPL-3.0 licence; fine for a hackathon, check before any commercial use).
- Build the TensorRT engine on the Jetson itself: `yolo export model=yolov8n.pt format=engine half=True imgsz=960`, then put `yolov8n.engine` here.
- Engines are tied to the exact Jetson + JetPack/TensorRT version, so don't share them between machines.
