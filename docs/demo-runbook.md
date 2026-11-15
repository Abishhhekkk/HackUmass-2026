# Judging demo runbook (about 3 minutes)

**Pitch line:** offline-first situational awareness for mass-casualty events. Detection is the
input; the product is a map that keeps working with no network and catches the cloud up safely.

## Setup (before judges arrive)
- Laptop A shows the **edge** dashboard (`:8080`, Jetson or laptop in mock mode). Laptop B or
  a second tab shows the **cloud** dashboard (Vultr URL or `:8000`).
- Have `scripts/demo_outage.py --fresh --keep` ready as the fallback if hardware misbehaves.
- Have your measured numbers on one slide: FPS on the Jetson, median/p90 position error from
  the field test, people matched vs. actual.

## Script
1. **Live** (20 s): the drone is flying, or a clip is replaying. People appear on both maps,
   chips show *Uplink online* and *Cloud up to date*.
2. **Cut the link** (40 s): click *Cut uplink*, or pull the Wi-Fi for real. The edge map keeps
   growing and the chip shows *Local v9 · cloud has v3 (pending)*. Point out that the cloud map
   is frozen but still usable.
3. **Bad connection** (20 s): click *Corrupt next upload*, then *Restore uplink*. The chip
   briefly shows the server's `checksum mismatch` rejection. Nothing half-written reaches the cloud.
4. **Catch-up** (20 s): the cloud jumps straight to the newest version. Only the newest
   snapshot is sent (a few KB), not the backlog and not video.
5. **Inspect** (40 s): click a dense cell (who's in it, confidence, error circles), switch the
   time filter, download the CSV, open the report. Mention the unlocated sightings during the
   GPS dropout: they're kept and flagged, never placed at the drone's position.
6. **AI summary** (optional, 20 s): Gemini writes a brief using only mission tools plus
   OpenStreetMap landmarks, labelled human-review-required.

## Questions to expect
- *How does it scale?* Every record carries `drone_id`. Several drones means several
  snapshot streams merged on the server, with the same "nearby + not seen at the same time"
  merge rule used on the Jetson.
- *How accurate?* Quote the field test numbers, not the synthetic ones. Error grows with
  altitude error and tilt; a rangefinder is the biggest single improvement.
- *What's "dynamic GPU allocation"?* The adaptive scheduler: it runs the detector on every 3rd–4th frame
  while searching, every frame while people are in view, and backs off when the Jetson is hot.
  The dashboard chip shows the current mode.
- *Why not stream video?* Bandwidth in a disaster zone is scarce. The result is a few KB;
  video waits for good signal.

## Limitations to state
Flat-ground and nadir-camera assumptions; RGB can't see through smoke or rubble; counts are
estimates (two people closer than ~4 m on different passes may be merged); the resource
allocation agent is advice for a human only.
