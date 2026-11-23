# Real-file behavior data from 520 GTA VI (Xbox Series) .bk2 files — KB2j coverage, 137 non-16-aligned edge vectors, console-side reference-decoder notes

Context: I've been processing the video corpus of the **retail GTA VI Xbox Series build** — 520 .bk2 files, 510 `KB2n` + 10 `KB2j`. Everything below is behavioral/statistical (no game media, no proprietary binaries, no keys). Figuring this might complement your real-file validation, which so far centers on 1440p/4K/720p `KB2n`.

## 1) Edge-flag test vectors: 26% of the corpus is NOT 16-aligned

Your PR #3 (row edge flag count from 16-aligned height) fixes the common case, but the files used there (`2560×1440`, 720p/4K) are all already 16-aligned. Header stats across the corpus (all 30 fps, flags=0x1) include:

| class | count | h%16 | w%16 | notes |
|---|---|---|---|---|
| 1920×1080 | 130 | 8 | 0 | the dominant real-world case |
| 1080×1080 | 1 | 8 | **8** | both dimensions unaligned |
| **1407×1600** | 1 | 0 | **15** | odd width — column-edge stress |
| 1635×720 | 1 | 0 | 3 | odd width |
| 2048×558 | 2 | 14 | 0 | |
| 2048×468 | 1 | 4 | 0 | |
| 1920×850 | 1 | 2 | 0 | |
| 720×1280 (portrait, in-game phone videos) | 72 | 0 | 0 | 16-aligned both dims; listed as transposed-aspect stress case, not counted below |

Totals: **137/520 files (26%)** have `h%16!=0 or w%16!=0` (the 72 portrait files are fully 16-aligned and are not counted). Would your current row-edge fix also predict correct **column**-edge flag counts for `1407×1600` and `1080×1080`? Those two would make excellent spec regression tests. Raw per-file table included at the end of this issue as a collapsible CSV block (520 rows, anonymized ids F000–F519, no filenames).

## 2) KB2j (integer variant 'j') observations

- 9 × `512×512`, 15 frames (arcade-cabinet minigame loops, 16-aligned both dims): decode cleanly except for the reproducible last-frame stall of §3 (14/15 frames emitted on all nine).
- 1 × `1920×1080`, 60 frames: consistent corruption confined to the **luma plane**; rendering chroma-only produces clean output. Tried: plane-order permutations (I420/YV12/NV12/VU12), 4/8-column Y deinterleave, 8×8 and 16×16 tile-order permutations, transposition, native vs self-allocated buffers — none fixed it. Given it's h%16=8 and one of your `SPEC-QUESTION`s is "edges past the flag list", could revision `j` predate the row-edge encoding your PR #3 formalized? Happy to re-test this exact file against candidate rules.

## 3) Console-side vendor decoder: cross-validation notes (behavior only)

The game ships a vendor decoder DLL (GXDK, June 2022 build) that is a plain x64 PE importing only CRT + `xmem.dll` (2 functions) + core Win32 + XAudio2. With a minimal allocator shim it runs on desktop Windows and served as our A/B reference against a clean pipeline. Interface behavior worth noting for your spec:

- registered-buffer dimensions are **reported with 16-aligned padded height/width** (1080 → 1088 rows; 720 → 736 cols for portrait), and the padding region contains **edge-replicated rows/columns** — consistent with your PR #3 finding;
- the registered frame-buffer struct's chroma slot pointers are swapped relative to their names (the `uptr` slot carries **V**) — caught a U/V swap bug in our pipeline via A/B;
- audio: DCT v2 at 48 kHz stereo, ~6.4 KB decoded bytes/frame at steady state on single-track files; the first `BinkGetTrackData` call returns a bulk block (~146 KB ≈ 23 frames' worth) rather than one frame's worth, while the cumulative total still matches video duration exactly. (Corroborates your PR #2's 99.92% match result, from the console-side build rather than PC RAD tooling.)
- last-frame stall: on 10/520 files (all nine 512×512@15f `KB2j` arcade loops plus one `KB2n` 1920×1080@150f) the final `BinkDoFrame` stalls ~26 s, then the decode loop terminates gracefully **without emitting the last frame** (N-1 output, no error code); reproducible per-file, not timeout-related.
- multi-track containers: **25/520 files carry multiple independent audio tracks** (24 with `Naud=2`, one with `Naud=4`; count at header `0x28`, per-track `u16 sample_rate + u16 flags` at `0x30+4N`; all 227 tracks 48 kHz, flags 0x7000 except three 0x3000), so a naive single-stream raw dump concatenates them into N× video duration (each track `frames×6400 B`). On the sampled file track 0 is the program that aligns with picture; track 1 is an uncorrelated alternate program (sample corr ≈ 0.00, similar RMS). Consumers must select per track from the header table, not treat the concatenation as one stream. (The sampled multi-track file also shows a mild seam artifact: frame-boundary sample-diff energy ≈2× intra-frame, vs ≈1× on single-track files.)

## 4) Offer

I can run arbitrary patches/candidate spec rules against the ~510 real `KB2n` files (mixed resolutions incl. all edge classes above) and report per-file pass/fail + PSNR-vs-reference matrices. Everything stays behavioral — no media, no vendor binaries, no keys.

Thanks for the clean-room work either way — this decoder is the most useful public artifact in this format space right now.
