# yo_pull

Tools to view and record live video from a **YO v2.0** home sperm-test device on a Mac.

The camera looks through a counting slide; either the original or you can make one yourself (see below). Recordings can be used to visually inspect sperm and estimate concentration from manual counts.

## Quick start

1. Power on the YO device and join its **Yo2** WiFi network on your Mac.
2. Install ffmpeg: `brew install ffmpeg`
3. View and record live video:

```bash
python3 yo_view.py
```

4. Pull saved clips off the device (not actually very useful tbh):

```bash
python3 yo_pull.py setup
python3 yo_pull.py ftp-grab --out ~/Desktop/yo_videos
```

Press `q` in the viewer window to stop recording. Output defaults to `./yo_live_<timestamp>.mp4`.

## Scale

Calibrated from a reference image:

- **400 pixels = 0.2 mm**

| Quantity | Value |
|---|---|
| µm per pixel | **0.5 µm/px** |
| px per mm | **2000 px/mm** |

At the typical **640×480** video size, the full field of view is about:

- width: **0.32 mm** (640 px)
- height: **0.24 mm** (480 px)
- area: **0.0768 mm²**

### Slide gap

We assume the original YO slide has a **0.1 mm** sample gap (chamber depth). This has been assumed and only vaguely validated — treat concentration estimates as rough. A DIY slide using **80 gsm paper** as the spacer hits roughly the same depth (see [Making your own slide](#making-your-own-slide)).

### 1. How big should a sperm look?

| Part | Real size | Expected size in video |
|---|---|---|
| Head length | ~4–5 µm | **~8–10 px** |
| Head width | ~2.5–3 µm | **~5–6 px** |
| Full cell (head + tail) | ~50–60 µm | **~100–120 px** |

In a 640 px-wide frame:

- A sperm **head** is a small dot — roughly **1–2% of frame width**.
- A full motile sperm with tail can span up to **~15–20% of frame width**.
- Objects much wider than **~15 px** are probably debris, bubbles, or clumps, not individual heads.

## 2. Converting a slide count to M sperm/mL

**M sperm/mL** means **millions of sperm per millilitre** (same unit the YO app reports).

Count **N** sperm in a region of known area **A** (mm²). With slide gap **d = 0.1 mm**:

```
M sperm/mL = N / (A × d × 1000)
           = N / (A × 100)
```

### Full frame (640×480)

Using the whole visible field (`A = 0.0768 mm²`, `d = 0.1 mm`):

```
M sperm/mL ≈ N / 7.7
```

Example: **40 sperm** in the full frame → about **5.2 M/mL**.

### Typical reference range

Normal semen is often quoted around **15–200 M/mL**, with many fertile samples in the **40–80 M/mL** range. Use that to sanity-check manual counts.

## Making your own slide

The YO device is just a microscope over a thin sample chamber. The original slide is a precision spacer; you can approximate one with a standard microscope slide, a cover glass, and paper for spacing.

### Gap spacer: 80 g/m² paper

Normal **80 g/m²** (80 gsm) copy paper is about **0.1 mm** thick — close enough to the original slide gap to use the concentration formulas above without changing `d`.

Cut paper strip(s) to the width of your chamber. The paper sets the depth; the sample sits in the gap between the slide and cover glass.

### Tape thickness reference

If you experiment with tape as a spacer instead of paper, typical thicknesses are:

| Material | Typical thickness |
|---|---|
| Clear packing tape / carton sealing tape | 0.04–0.07 mm |
| Heavy-duty packing tape | 0.07–0.09 mm |
| Electrical PVC tape | 0.13–0.18 mm |
| Good-quality electrical tape, e.g. 3M Super 33+ class | ~0.18 mm |
| Cheap thin electrical tape | ~0.10–0.13 mm |

80 gsm paper (~0.1 mm) sits in the middle of this range and is a good default. Thinner tape gives a shallower chamber and may focus better but shifts your concentration math; thicker tape blurs more and also changes `d` — plug the actual thickness into `M sperm/mL = N / (A × d × 1000)`.

### Basic build

1. **Bottom** — standard **microscope slide** (75 × 25 mm).
2. **Spacer** — 80 gsm paper strip(s) taped along the edges (or two parallel strips) to create a shallow channel in the middle.
3. **Sample** — a small drop of semen in the channel. Keep it thin; you want a single layer, not a pool.
4. **Top** — standard **cover glass** laid on the spacers, pressing gently so the gap is even.

Tape the stack at the edges so it does not slide apart when you insert it into the device.

### Tips

- **Match the original slide footprint** if you can — trim so the stack sits flat in the YO holder and the camera looks through the centre of the channel.
- **Thicker gap = worse focus.** The camera has limited depth of field; a gap much above ~0.1 mm will look soft and sperm will be harder to see and count. Stick to one sheet of 80 gsm paper.
- **Avoid bubbles** under the cover glass — they dominate the image and ruin counts.
- **One paper thickness = one gap.** Stacking two sheets ≈ 0.2 mm and will throw off concentration by ~2× as well as blur the image further.

This is a rough substitute, not a clinical-grade chamber. Use it for visual inspection and ballpark counts, not diagnosis.

## Files

| File | Purpose |
|---|---|
| `yo_view.py` | Live view + record from RTSP |
| `yo_pull.py` | Download clips from device (HTTP/FTP/API) |

## Trimming recordings

LosslessCut is the easiest GUI option for time-cropping MP4s on macOS:

```bash
brew install --cask losslesscut
```

Or with ffmpeg:

```bash
ffmpeg -ss 00:00:05 -to 00:00:20 -i yo_live_20260621_002523.mp4 -c copy cropped.mp4
```

## TODO

Obviously the counting an evaluating should be done by AI these days.