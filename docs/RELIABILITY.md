# Reliability summary — 520-file corpus sweep

Method: two-stage sweep over a 520-file `.bk2` corpus (6.2 GB source bytes,
344,448 frames total) carved from RPF9 archives by 4096-aligned `KB2` magic
scan with header-sanity filtering.

## Stage A — decode-stream integrity (all 520 files)

Raw decode stream piped through `ffmpeg signalstats` to a null sink (no media
written). Per file: decoded frame count vs header frame count, both process
exit codes, and per-frame YAVG/UAVG/VAVG/SAT statistics.

- 510/520 `ok` (full frame count, rc=0/0).
- 10/520 `last-frame stall`: the final `BinkDoFrame` stalls ~26 s, then the
  decode loop exits gracefully emitting N-1 frames (nine 512×512@15f `KB2j`
  arcade loops + one 1920×1080@150f `KB2n`). Reproducible per file; not
  timeout-related.
- 0 unplanned failures.

Outlier adjudication (luma extremes, chroma cast, saturation extremes): every
flag resolved to authored content (neutral-chroma black/white bumpers,
brand-colored UI screens, stylized high-saturation graphics, warm court
scenes with natural skin tones). Zero decode artifacts. Visual spot checks on
12 frames across all flagged classes and stratified representatives.

## Stage B — full contract re-verification (43 stratified files)

One file per distinct (w,h,codec) class + all Stage-A anomalies + 15 random
(seed 20261124), converted end-to-end through the skill (`convert` with
ffprobe three-point contract: frame count vs header ±1, |duration −
frames/fps| < 0.05 s, audio loudness when a track exists with silent-source
downgrade).

- 43/43 `ok`, zero anomalies.
- Odd-width sources (1407×1600, 1635×720) now encode via even-dimension crop
  from the 16-aligned padded buffer (edge-replicated +1 column), noted in JSON
  output.

## Audio findings

- Sync: audio-vs-video envelope lag 0.000 s on all tested files (correlation
  ≥ 0.9986).
- Multi-track containers: 25/520 files carry N>1 independent audio tracks
  (24×N=2, 1×N=4; track table at header `0x28`, per-track u16 rate + u16 flags
  at `0x30+4N`; all 227 tracks 48 kHz). A single-stream dump concatenates them;
  selection must be per track. Track 0 aligns with picture on the sampled
  file.
- Silent sources: several files carry all-zero PCM tracks; verified via raw
  PCM peak = 0 and labeled, not treated as pipeline faults.
- One multi-track file shows a mild seam artifact (frame-boundary sample-diff
  ≈2× intra-frame vs ≈1× on single-track controls); cause unknown, left
  unmodified.

## Header statistics (corpus-wide, confirmed)

- Variants: 510 `KB2n`, 10 `KB2j`; fps 30 in 520/520; flags 0x1 in 520/520;
  frame-count duplicate at `0x10` equals `0x08` in 520/520.
- 137/520 files have `h%16 != 0 or w%16 != 0` (incl. 1407×1600 with w%16=15 and
  1080×1080 with both dims unaligned); 72 portrait 720×1280 files are fully
  16-aligned.
- Audio track count distribution: N=0 in 320/520, N=1 in 175, N=2 in 24, N=4
  in 1.

All numbers reproducible from `data/bink2_headers_anon.csv` plus the sweep
driver in `tools/sweep.py`.
