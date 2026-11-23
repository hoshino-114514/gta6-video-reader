---
name: bink2mp4
description: Convert Bink2 (.bk2) videos from RAGE-engine game data into standard H.264 MP4 with verified audio, and carve .bk2 streams out of RAGE .rpf archives. Use when the user has .bk2 or .rpf files and wants playable/verifiable MP4s. Requires Windows x64 and ffmpeg/ffprobe on PATH.
---

# bink2mp4

Convert private Bink2 (`.bk2`) video to standard H.264 MP4, extract `.bk2`
streams from RAGE `.rpf` archives, and auto-verify every result against a
three-point contract (frame count, duration, audio loudness).

## Prerequisites

- Windows x64 (bundled decoder is a Win64 binary; see
  `references/pipeline.md` §5 to rebuild elsewhere with MinGW-w64).
- `ffmpeg` and `ffprobe` on `PATH` — the script probes with `shutil.which`
  and prints a generic install hint if missing; install them yourself.
- Python ≥ 3.9, stdlib only. No hardcoded data paths: every input/output
  location is a CLI argument you fill in for your machine.

## Usage

Entry point (run from anywhere; the script self-locates its `assets/bin`):

```
python <skill-dir>/scripts/bink2mp4.py probe   "<your file>.bk2"
python <skill-dir>/scripts/bink2mp4.py convert "<your file>.bk2" -o "<out dir>/out.mp4" [--crf 20] [--no-audio] [--no-verify]
python <skill-dir>/scripts/bink2mp4.py batch   "<dir with bk2s>" -o "<out dir>" [--limit N] [--resume] [--only KB2n|KB2j|all]
python <skill-dir>/scripts/bink2mp4.py extract "<archive.rpf or dir of rpf>" -o "<out dir>" [--only KB2n|KB2j|all] [--limit N]
python <skill-dir>/scripts/bink2mp4.py status  "<out dir>"
```

`probe` prints one JSON object; `convert`/`batch`/`extract` emit JSON Lines
(one record per job: `file,status,dur_s,frames,audio,vsize,note`); errors go to
stderr. Output paths are always explicit arguments.

## Hard constraints

1. **cwd dependency**: the decoder loads its DLLs from the current directory —
   the script runs it with `cwd=<skill-dir>/assets/bin`; never move the exe
   away from its DLLs.
2. **Timeout policy**: `BinkDoFrame` can hard-hang on some files (typically a
   KB2j last frame). Every decoder call is wrapped in a timeout (default
   `max(120 s, 0.5 s × frames)`, override with `--timeout`); timeouts mark the
   job `failed`, retried at most once.
3. **Copyright**: the bundled third-party Bink2 DLL may be used only on a
   machine that legally holds the game data being converted. Do not
   redistribute the binaries or the converted media.

## More

Decoder API semantics, header layout, the full pitfall list, RPF carving
details, and rebuild instructions: `references/pipeline.md`.
