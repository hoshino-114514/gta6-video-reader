# video / ttpf / rpf9 / script-resources: Bink2 header fields + variant behavior, RDS5 page-stream layout, TOC cryptanalysis negative results, PSIN record shape

Proposed documentation contributions from hands-on processing of the **retail GTA VI Xbox Series build** (520 video files, 57 ttpf stores, 22 RPF9 archives). Behavior/stats only — no game files, no keys, no proprietary binaries, consistent with LEGAL.md. Section → target page: **A** → the Bink2/video formats page; **B** → `structure/05-ttpf-stores.md`; **C** → `structure/01-rpf9-container.md`; **D** → the script-resource page. Confidence tags follow repo convention (confirmed / inferred / unknown).

## A. Video (Bink2) — facts verified on real retail files

### A.1 File header (little-endian)
| offset | field | verified notes |
|---|---|---|
| 0x00 | magic `"KB2"` + variant char | observed variants in this corpus: `j`, `n` |
| 0x04 | u32 declared_size | byte-exact span of the stream; following bytes are zero padding to 0x1000 |
| 0x08 | u32 frame_count | |
| 0x0C | u32 largest_frame | |
| 0x10 | u32 frame_count (dup) | always == 0x08 in 520/520 files |
| 0x14 | u32 width | |
| 0x18 | u32 height | |
| 0x1C | u32 fps | 30 in 520/520 (confirmed) |
| 0x20 | u32 flags | 0x1 in all files |

Then the audio track table: `0x28` u32 track count N (observed distribution: N=0 in 320/520, N=1 in 175, N=2 in 24, N=4 in 1), `0x2C` u32 revision-extra, `0x30` N×4 max-decoded-size, `0x30+4N` N×(u16 sample_rate + u16 flags) — all 227 tracks 48000 Hz; flags 0x7000 (stereo|DCT|16-bit) in 224 tracks, 0x3000 in 3 — then `0x30+8N` track ids. The frame index (u32 offsets, monotonic) begins at `0x30+12N`, i.e. only track-less files have it immediately after the fixed header. Padding between files is 4 KiB aligned: observed `next_off - off == ceil(declared_size / 4096) * 4096` for the back-to-back arcade minigame loops.

### A.2 Variant behavior (n vs j)
- `KB2n`: all spot-checked files decode consistently (confirmed, n=10 converted end-to-end with vendor-decoder A/B, covering 720p/1080p/1440p/1080×1080/portrait 720×1280 in-game phone videos). Full-corpus decode not attempted; corpus-scale consistency: unknown.
- `KB2j`: 9 small 512×512 arcade loops decode with a reproducible last-frame stall (§A.3; 14/15 frames emitted on all nine); a single 1920×1080@60f file shows corruption **confined to the luma plane** (chroma-only rendering is clean). Plane-order permutations, 4/8-column Y deinterleave, 8×8/16×16 tile orders, transpose all ruled out. Its `h%16==8` suggests the row/edge-flag encoding question raised in bink2-decoder PR #3 territory — possibly `j` predates the current edge-flag rule (inferred).

### A.3 Console-side vendor decoder — interface behavior (for cross-validation methodology)
The game ships a vendor decoder DLL (GXDK, June 2022 build) that is a plain x64 PE importing only CRT + `xmem.dll` (2 funcs) + core Win32 + XAudio2. With a minimal allocator shim it runs on desktop Windows — useful as a *same-machine reference output* when validating clean-room decoders. Observed interface behavior worth documenting:
- frame-buffer info reports **16-aligned padded dimensions** (e.g. 1080→1088 rows, 720→736 cols for portrait); padded region contains edge-replicated rows/columns. Crop by the file-header dimensions, not the runtime-reported ones.
- the registered-buffer struct's chroma slot pointers are **name-swapped: the `uptr` slot actually carries V**. An I420 assembly trusting slot names produces red/blue-inverted output (symptom: blue skin).
- audio (DCT v2 @48 kHz stereo): steady-state ~6.4 KB decoded bytes/frame on single-track files; the first `BinkGetTrackData` call returns a bulk block (~146 KB ≈ 23 frames' worth) rather than one frame's worth, while the cumulative total still matches video duration exactly.
- multi-track files (confirmed, 25/520: 24×N=2, 1×N=4): a single-stream raw dump concatenates the tracks into N× video duration; selection must be per track from the `0x28` table. On the sampled file track 0 aligns with picture while track 1 is an uncorrelated alternate program (sample corr ≈ 0.00, similar RMS).
- last-frame stall (confirmed, 10/520: all nine 512×512@15f `KB2j` loops + one `KB2n` 1920×1080@150f): the final `BinkDoFrame` stalls ~26 s, then the decode loop exits gracefully emitting N-1 frames; no error code, not timeout-related.
- no-audio tracks: verify with triple evidence — track count 0, no audio-bit in track table, `GetTrackData` returns 0 bytes on every frame. (The header track count at `0x28` gives a direct cross-check: N=0 in 320/520 files, matching the triple-evidence classification. A single signal misleads: 3 of 10 spot-checked files were genuinely silent sources.)

### A.4 Storage layout inside RPF9 (structure/01 territory)
Video streams sit back-to-back at **4096-aligned offsets** with zero padding to the next 4 KiB boundary (confirmed: `next_off − off == ceil(declared_size/4096)·4096` on adjacent pairs, e.g. the arcade minigame loops). Stream payloads are **unencrypted even where the archive TOC is encrypted** (confirmed: verification method = locating `KB2` magic at aligned offsets and checking the header `declared_size` spans the stream byte-exactly, with no directory access).

## B. ttpf / RDS5 — container structure (fully mapped, cross-checked on 3 stores)
- `0x00` magic `RDS5`; `0x04` u32 entry_count; `0x08` u32 total (virtual) size; then a small key-value table: entries shaped `key = 0x60000000|id`, `val = (u16 payload << 16) | (4*seq)`.
- `0x100` second header `ETPF`: its page-count field **equals the number of stream pages actually present** (verified: 5→5, 1→1).
- Payload = **zlib page stream** starting at `0x1000`: pages are 0x100-aligned with zero padding between; page sizes are powers-of-two multiples of 4096. Sampled store: 663 pages / 81.8 MB inflated from 21 MB file.
- So ttpf "opaque 8.0-entropy payloads" are just compressed pages; the real unknown remains the post-inflate tile bitstream (= BCPack territory; our negative results on BC1/BC3/Morton hypotheses are summarized in bink2-decoder discussions thread if useful).
- Related: `textures.rpf` inflated streams are the same tile-DB family (0x5555/0xaaaa-patterned metadata records + bitstream tiles).

## C. RPF9 TOC encryption — negative results (so others don't repeat the path)
- Header (magic `RPF9`, entry counts, name-table length) is plaintext; TOC region entropy ≈ payload compression but names table shows no plaintext.
- Constant 32-byte ciphertext block at TOC+0x40 across archives, yet: cross-archive XOR of entry regions shows ~0 zero-rate → **no keystream reuse exploitable**; autocorrelation over 256 B lags shows no period → not fixed-pad XOR. Practical conclusion: TOC recovery needs per-title key material (absent from data-only dumps).
- Caveat kept honest: analysis used ~6 small archives + header windows of the large ones.

## D. PSIN (script resources)
- 2309 PSIN resources inventoried in script streams: header `PSIN` + `0x0000` filler, then **16-byte records `(u32 key, u32 value, 8×0xaa pattern)`**. No texture dictionary found inside PSINs (the power-of-two (w,h) pairs seen there are script struct coincidences).

## Open questions
- ttpf post-inflate tile bitstream: **unknown** (BCPack-family inferred; BC1/BC3 and Morton/tile-order hypotheses ruled out on sampled pages — negative results).
- KB2j 1080p luma-plane corruption: **unknown** (reported to bink2-decoder separately); whether revision `j` predates the current row-edge rule: inferred.
- RPF9 TOC recovery: **unknown**; per-title key material appears required (cryptanalysis negative results in §C are confirmed on the sampled set only).
- cause of the mild per-file audio seam artifact (frame-boundary sample-diff ≈2× intra-frame on the sampled multi-track file vs ≈1× on single-track controls): **unknown**.

---
Source of all numbers: reproducible header/page stats (scripts available). Happy to open a PR into `structure/` with test fixtures expressed as byte-pattern assertions (no copyrighted bytes) if this repo prefers merged docs over an issue dump.
