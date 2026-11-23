# bink2mp4 pipeline — technical knowledge

Machine-neutral reference for the Bink2 (`.bk2`, RAGE engine) → H.264 MP4
pipeline and RPF archive carving. Paths are placeholders supplied by the
operator; nothing here assumes a particular host layout.

## 1. bk2 header fields (plain Python read, no DLL needed)

| offset | type   | meaning |
|--------|--------|---------|
| 0      | 4 bytes| magic `"KB2"` + version char (commonly `n` / `j`) |
| 4      | u32 LE | total_size (whole-file declared size) |
| 8      | u32 LE | frames |
| 12     | u32 LE | largest_frame |
| 16     | u32 LE | frames2 (usually == frames) |
| 20     | u32 LE | width |
| 24     | u32 LE | height |
| 28     | u32 LE | fps (integer) |
| 32     | u32 LE | flags |

True width/height come from the header. The decoder's fbinfo reports
16-pixel-edge-padded values (e.g. real 720 shown as 736, real 1080 as 1088) —
always crop to the header truth. Exception: odd header dims get an even +1
crop instead (padding pixels are edge-replicated) — see pitfall 11.

## 2. Decoder invocation contract (`binkdec_raw.exe` v7, per its C source)

Positional argv: `<bk2> <frames> <outdir> <tag> <mode> [opts...]`.
`<outdir>`/`<tag>` are only used by the legacy BMP mode; pass placeholders
(e.g. `.` and `v`). The binary is at `<skill>/assets/bin` and **must run with
cwd = that directory** (it resolves `Bink2Scarlett.dll` etc. from cwd).

- Video (I420 on stdout):
  `binkdec_raw.exe <in.bk2> <frames> . v raw crop:<W>x<H>`
  Emits tightly-packed I420 (Y + U + V, chroma slots already corrected) to
  stdout; all its own logs go to stderr in this mode (fd is force-set binary —
  no `\n`→`\r\n` inflation).
- Audio (WAV **file**, not stdout):
  `binkdec_raw.exe <in.bk2> <frames> . a aud wav:<abs-out.wav>`
  Writes a 44-byte-header PCM WAV (observed: 48000 Hz stereo s16le, ~6400 B per
  video frame, first frame priming 145920 B). `DONE okframes=N` prints on
  stdout in this mode.
- Mux (video pipe + audio file):
  `exe raw … | ffmpeg -f rawvideo -pix_fmt yuv420p -s WxH -r FPS -i -
   [-i out.wav] -c:v libx264 -preset medium -crf CRF -pix_fmt yuv420p
   [-c:a aac -shortest] <out.mp4>`
- Optional extra argv recognized (source-verified): `goto:<N>` (seek, slot 7),
  `allocfb` (DLL-allocated buffers) and `raw:<file>` (file instead of stdout).

## 3. Pitfalls (all previously observed; keep in force)

1. **CRT text-mode pipe corruption**: without `_O_BINARY` the stdout data
   stream inflates via `\n`→`\r\n` (caused false-positive passes once). The exe
   forces binary on its raw fd; any *re-implementation* must likewise transfer
   stdout in binary — recommended: connect exe stdout directly to ffmpeg stdin,
   never through Python text layers.
2. **fbinfo padding**: width/height from the decoder include 16-align padding
   (1088 rows / 736 cols seen). Crop to header truth.
3. **Chroma slot crossover**: in the registered framebuffer the `uptr` slot
   actually holds **V** and `vptr` holds **U** (YV12-internal). Do not infer
   order from pointer names when reimplementing; the shipped exe already emits
   corrected I420.
4. **Audio API semantics**: `BinkGetTrackType(handle,idx)` 0=video 1=audio;
   `BinkOpenTrack` **requires** a prior `BinkOpenXAudio2("default",0)` or it
   crashes; `BinkGetTrackData(track,buf,cap)` returns this-frame byte count.
   Observed stream: 48000 Hz stereo s16le, 6400 B/frame, first frame 145920 B.
   A/V length is aligned with `-shortest`.
5. **No-audio verdict needs triple evidence**: track count 0 / type table has
   no audio entries / per-frame `GetTrackData` = 0 bytes. This pipeline checks
   opened-track count and total WAV data bytes.
6. **`BinkDoFrame` can hard-hang on individual files** (classically the last
   frame of KB2j). Wrap every decoder call in a timeout (60–120 s per phase,
   scaled by frame count), record `failed` on expiry, retry ≤ 1. A hang at the
   final frame is tolerated by the ±1 frame-count contract below. Related
   vendor-decoder variant: the final `BinkDoFrame` can stall for an extended
   period (~26 s observed), then the decode loop exits **gracefully** (rc=0,
   no error) but **skips the last frame** — the pipeline yields N-1 frames.
   This is not a timeout; verify's ±1 tolerance keeps `status=ok` and
   annotates `last-frame-stall (vendor decoder skipped final frame)`.
7. **DLLs load from cwd**: `subprocess` cwd must be the bin directory.
8. **Known unsolved**: KB2j codec at large resolutions shows luma banding
   artifacts (512² renders clean). For batches prefer `--only KB2n`, or label
   such outputs `flawed` and deliver with the caveat.
9. **Verification is the contract** (all three, on the produced mp4):
   ① `ffprobe` nb_frames == header frames ±1 (last-frame hang annotated);
   ② |duration − frames/fps| < 0.05 s;
   ③ when an audio stream exists, `ffmpeg -af volumedetect` mean_volume > −90 dB.
   ③ admits one probe-based downgrade: before muxing, read the extracted WAV's
   raw PCM (stdlib `wave`, s16le) and compute max abs sample; if the *source*
   audio is already digital silence (peak == 0), a ≤ −90 dB mean_volume in the
   mp4 reflects the source, not a mux defect — record `status=ok` with note
   `source-audio-silent (raw PCM peak=0)`. If source PCM peak > 0, ≤ −90 dB
   still fails (real mux defect).
10. **Copyright red line**: the official Bink2 DLL redistributed here may only
    be used on machines that legally hold the corresponding game data. Do not
    distribute the third-party binaries or the converted game media.
11. **Odd width/height sources crash the encoder**: yuv420p + libx264 require
    even dims. Feeding an odd header size (e.g. 1407x1600, 1635x720) as
    rawvideo passes ffmpeg's rawvideo *decoder* fine but libx264 fails at
    encoder open ("Generic error in an external library"; ffmpeg exits with
    unsigned rc 3752568763 = 0xDFABA7BB) and a 0-byte mp4 is left behind.
    Fix (implemented in `do_convert`): when w or h is odd, request
    `crop:(w+1)x(h+1)` from the decoder — the 16-aligned framebuffer padding
    holds edge-replicated pixels, so the extra row/col is real picture edge,
    not garbage (verified: added column vs. its neighbor differs by ≤2 luma
    units summed over all rows, 0 for chroma). The even crop always fits the
    padded buffer (odd w ⇒ align16(w) ≥ w+1). Output is 1 px larger than the
    header truth and the JSON record carries
    `note="odd-source padded to WxH (edge-replicated)"`. Verified fallback if
    the crop route is ever unavailable: `-vf scale=<even>:<even>` on the
    ffmpeg side (resamples every pixel; the crop route is lossless, prefer it).

## 4. Carving bk2 from RAGE RPF archives

RPF headers themselves are plaintext (`"RPF"` magic + version + entry
count/name table/flags) but the entry names and directory are encrypted —
extraction ignores them entirely and is pure payload scanning:

- scan offsets at 4096-byte alignment for the `"KB2"` magic;
- accept a hit only if its header validates: `total_size ∈ (0, 200 MB]`,
  `frames ≤ 4e6`, `w ≤ 7680`, `h ≤ 4320`, `fps ∈ [1, 240]`, and
  `offset + total_size ≤ file_size`;
- slice `total_size` bytes at that offset;
- dedupe: a later hit whose offset falls inside an already-accepted span is
  skipped.

The `extract` subcommand implements exactly this; `--limit N` bounds scan work
on huge archives, `--only KB2n|KB2j` filters by version byte.

## 5. Rebuilding the decoder on another machine

`assets/bin` ships Windows x64 binaries. On non-Windows/x64 hosts, or to patch
behavior, rebuild from `assets/src` with MinGW-w64 (`__int64`, `VirtualAlloc`,
`LoadLibraryA` are used; no MSVC-only extensions beyond these):

```
gcc -O2 -static -o binkdec_raw.exe binkdec.c
gcc -O2 -shared -o xmem.dll xmem.c
```

`Bink2Scarlett.dll` is the proprietary runtime — it cannot be compiled from
these sources; it (plus `XAudio2_9.dll` and the VC++ runtime DLLs) must be
present beside `binkdec_raw.exe`, obtained from a legally-owned copy of the
game. The `xmem.dll` stub satisfies its import surface with page-aligned
`VirtualAlloc`.
