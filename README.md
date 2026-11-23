# gta6-video-reader

Read and convert Bink2 (`.bk2`) video streams stored inside RAGE `RPF9` archives
(GTA VI Xbox Series data layout), and document the container/codec behaviors
observed on real retail files.

## What this repository is

A personal archive of a working, verified pipeline plus the format knowledge it
produced:

- `skill/` — a portable, machine-neutral skill package: one Python entry point
  (`scripts/bink2mp4.py`) with `probe / convert / batch / extract / status`
  subcommands, the C source of the decode host and allocator stub
  (`assets/src/`), and `references/pipeline.md` (API semantics, pitfall list,
  verification contract).
- `tools/sweep.py` — full-corpus reliability sweep driver (decode-stream
  integrity + per-frame luma/chroma statistics + stratified full-contract
  re-verification).
- `data/bink2_headers_anon.csv` — anonymized header statistics for a 520-file
  corpus (ids `F000`–`F519`; no filenames, no paths).
- `docs/` — format/behavior notes and two upstream-issue drafts
  (`contrib_bink2_issue.md`, `contrib_assetinfo_docs.md`), plus
  `RELIABILITY.md` (verification summary).

## What this repository is NOT

- No game media (no `.bk2`, no video, no frames, no audio).
- No proprietary binaries. The decode path drives the vendor Bink2 DLL that
  ships with the game; this repo contains only our own host code and stub
  sources. You must supply that DLL yourself, on a machine where you lawfully
  own the game data.
- No keys, no decryption material, no archive directory/TOC recovery tools.
- No machine-specific paths or identities anywhere; every location is passed
  by the caller at runtime.

## Requirements

- Windows x64 (the decode host and vendor DLL are x64 PE).
- `ffmpeg` / `ffprobe` on `PATH`.
- Python ≥ 3.9, standard library only.
- Build the decode host from `skill/assets/src/` with any x64 toolchain; see
  `skill/references/pipeline.md` for the decoder call contract and the
  allocator stub signature.

## Quick start

```
python skill/scripts/bink2mp4.py probe   <your.bk2>
python skill/scripts/bink2mp4.py convert <your.bk2> -o <out.mp4>
python skill/scripts/bink2mp4.py batch   <bk2-dir> -o <out-dir> --resume
python skill/scripts/bink2mp4.py extract <rpf-file-or-dir> -o <out-dir>
```

`convert` muxes video (H.264) and audio (AAC) and self-verifies with ffprobe:
frame count vs header, duration vs frames/fps, and audio loudness when a track
exists (with a documented downgrade for genuinely silent sources).

## Verified behaviors (highlights)

See `docs/RELIABILITY.md` for the full 520-file sweep and `docs/contrib_*.md`
for the detailed notes: 16-aligned padded frame buffers with edge replication,
name-swapped chroma slots, per-file last-frame stall (10/520), multi-track
audio containers (25/520), odd-dimension sources, and RPF9 storage layout
facts.

## License

MIT — see `LICENSE`. Applies to the code and notes in this repository only;
the vendor decoder and the game data remain property of their respective
owners.
