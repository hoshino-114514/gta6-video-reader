#!/usr/bin/env python3
# bink2mp4.py — convert Bink2 (.bk2) videos to H.264 MP4; extract .bk2 from RAGE
# RPF archives. Machine-neutral: locates its bundled decoder via __file__, takes
# all data paths from the caller. stdlib only; requires ffmpeg/ffprobe on PATH.
#
# Decoder CLI (verified against assets/src/binkdec.c, v7):
#   binkdec_raw.exe <bk2> <frames> <outdir> <tag> raw crop:WxH   -> I420 on stdout
#   binkdec_raw.exe <bk2> <frames> <outdir> <tag> aud wav:P      -> WAV file at P
#   (positional <outdir>/<tag> only matter in BMP mode; pass placeholders.
#    In raw mode the exe reroutes its own logs to stderr and forces binary stdout.
#    aud mode writes a 44-byte-header WAV to the given path, NOT to stdout.)
# The exe must run with cwd = assets/bin (its DLL deps load from cwd).

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = SKILL_ROOT / "assets" / "bin"
EXE = BIN_DIR / "binkdec_raw.exe"

HDR_FMT = "<8I"  # total_size, frames, largest_frame, frames2, w, h, fps, flags
MAX_SCAN_SLICE = 200 * 1024 * 1024


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def need_tools(with_ffprobe=True):
    if not EXE.exists():
        die(f"decoder not found at {EXE}; see references/pipeline.md to rebuild", 2)
    ff = shutil.which("ffmpeg")
    if not ff:
        die("ffmpeg not found on PATH; install ffmpeg (x64) and make sure ffmpeg.exe resolves", 2)
    fp = shutil.which("ffprobe") if with_ffprobe else None
    if with_ffprobe and not fp:
        die("ffprobe not found on PATH; it ships with ffmpeg — install/relocate accordingly", 2)
    return ff, fp


def read_header(path):
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        magic = f.read(4)
        rest = f.read(32)
    if len(magic) < 4 or magic[:3] != b"KB2":
        die(f"{p.name}: not a KB2 file", 1)
    if len(rest) < 32:
        die(f"{p.name}: truncated header", 1)
    v = struct.unpack(HDR_FMT, rest[:32])
    codec = magic[3:4].decode("latin1")
    if not codec.isprintable():
        codec = f"0x{magic[3]:02x}"
    return {
        "path": p, "fsize": size, "codec": codec,
        "declared_size": v[0], "frames": v[1], "largest": v[2],
        "frames2": v[3], "w": v[4], "h": v[5], "fps": v[6], "flags": v[7],
    }


def validate_header(h):
    errs = []
    if not (0 < h["frames"] <= 4_000_000):
        errs.append(f"frames={h['frames']}")
    if not (0 < h["w"] <= 7680 and 0 < h["h"] <= 4320):
        errs.append(f"size={h['w']}x{h['h']}")
    if not (1 <= h["fps"] <= 240):
        errs.append(f"fps={h['fps']}")
    return errs


# ---------------------------------------------------------------- decode steps

def decode_audio(bk2, frames, wav_path, timeout):
    """Run exe aud mode -> WAV file. Returns (n_tracks, data_bytes, okframes, err)."""
    wav_path = Path(wav_path)
    log = wav_path.with_suffix(".log")
    cmd = [str(EXE), str(Path(bk2).resolve()), str(frames), ".", "a", "aud",
           f"wav:{wav_path.resolve()}"]
    with open(log, "wb") as lf:
        try:
            # aud mode uses no stdout data stream; "DONE okframes=" lands on stdout
            r = subprocess.run(cmd, cwd=str(BIN_DIR), stdout=lf,
                               stderr=lf, timeout=timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            return (0, 0, 0, "audio decode timeout")
    text = ""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        pass
    log.unlink(missing_ok=True)
    ntracks = len(re.findall(r"^AUD trk", text, re.M))
    m = re.search(r"okframes=(\d+)", text)
    okframes = int(m.group(1)) if m else 0
    data_bytes = 0
    if wav_path.exists() and wav_path.stat().st_size >= 44:
        with open(wav_path, "rb") as f:
            f.seek(40)
            d = f.read(4)
        if len(d) == 4:
            data_bytes = struct.unpack("<I", d)[0]
    return (ntracks, data_bytes, okframes, None if rc == 0 or okframes else f"aud rc={rc}")


def pipe_video(bk2, frames, w, h, fps, out_mp4, crf, wav_path, ff, timeout):
    """exe raw -> pipe -> ffmpeg -> mp4. Returns (okframes, err, note).
    w/h must be the ENCODE size (even — libx264 yuv420p rejects odd dims);
    for odd-dim sources the caller passes header dims +1, which the decoder
    serves from its 16-aligned edge-replicated padding via crop:(w+1)x(h+1)."""
    out = Path(out_mp4)
    cmd_ff = [ff, "-y", "-loglevel", "error",
              "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
              "-r", str(fps), "-i", "-"]
    if wav_path:
        cmd_ff += ["-i", str(Path(wav_path).resolve())]
    cmd_ff += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
               "-pix_fmt", "yuv420p"]
    if wav_path:
        cmd_ff += ["-c:a", "aac", "-shortest"]
    cmd_ff += [str(out.resolve())]
    cmd_exe = [str(EXE), str(Path(bk2).resolve()), str(frames), ".", "v", "raw",
               f"crop:{w}x{h}"]
    log = out.with_suffix(out.suffix + ".log")
    ff = None
    try:
        with open(log, "wb") as lf:
            ff = subprocess.Popen(cmd_ff, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=lf)
            ex = subprocess.Popen(cmd_exe, cwd=str(BIN_DIR), stdout=ff.stdin,
                                  stderr=lf)
            ff.stdin.close()
            vid_err = None
            try:
                ex.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                ex.kill()
                try:
                    ex.wait(10)
                except subprocess.TimeoutExpired:
                    pass
                vid_err = "video decode timeout"
            try:
                frc = ff.wait(timeout=120)
            except subprocess.TimeoutExpired:
                ff.kill()
                return (0, "ffmpeg timeout", "")
            ff = None
            if vid_err is None and frc != 0:
                vid_err = f"ffmpeg rc={frc}"
    finally:
        if ff is not None:
            try:
                ff.kill()
            except OSError:
                pass
    text = ""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        pass
    log.unlink(missing_ok=True)
    m = re.search(r"okframes=(\d+)", text)
    okframes = int(m.group(1)) if m else 0
    note = ""
    m = re.search(r"DoFrame timeout at frame (\d+)", text)
    if m:  # known KB2j end-frame hang; verify() applies the ±1 tolerance
        note = f"DoFrame hang at frame {m.group(1)}, emitted {okframes} frames"
    return (okframes, vid_err, note)

def wav_peak_abs(wav_path):
    """Max abs s16le sample value (peak) of a WAV's PCM data; None if the file
    is unreadable or not s16le. Downstream only distinguishes peak==0 from
    peak>0, so scanning exits early once any nonzero byte is seen (a zero
    s16le sample is two zero bytes, hence peak==0 <=> all data bytes zero)."""
    try:
        with wave.open(str(wav_path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            while True:
                buf = w.readframes(1 << 15)
                if not buf:
                    return 0
                if any(buf):
                    return 1
    except (wave.Error, OSError, EOFError):
        return None


# ---------------------------------------------------------------- verification

def probe_json(ffprobe, mp4):
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,nb_frames,duration:format=duration",
             "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def mean_volume(ffmpeg, mp4):
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(mp4), "-map", "a:0", "-af",
             "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"mean_volume[:=]\s*(-?[\d.]+)\s*dB", r.stderr)
        return float(m.group(1)) if m else None
    except subprocess.TimeoutExpired:
        return None


def verify(ffmpeg, ffprobe, mp4, frames, fps, expect_audio, silent_source=False):
    """Three-point contract: frame count, duration, audio loudness.
    Returns (notes, source_silent_hit, stall_hit). Contract ③ probe-based
    downgrade: when the pre-mux PCM was already all-zero (silent_source), a
    silent mp4 audio track is not a mux defect — no failure note is raised and
    source_silent_hit is set so the caller can annotate the ok record.
    A ≤ −90 dB track with nonzero source PCM stays a failure.
    Contract ① ±1 tolerance: nb_frames == frames-1 stays ok but sets
    stall_hit — the known vendor-decoder last-frame stall (final BinkDoFrame
    stalls, then exits gracefully without emitting the last frame) — so the
    caller annotates the ok record."""
    notes = []
    source_silent_hit = False
    stall_hit = False
    info = probe_json(ffprobe, mp4)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    astreams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    nf = vs.get("nb_frames")
    if nf is not None:
        if abs(int(nf) - frames) > 1:
            notes.append(f"nb_frames={nf} vs header {frames}")
        elif int(nf) == frames - 1:
            stall_hit = True
    elif frames > 1:
        notes.append("nb_frames absent in container")
    dur = vs.get("duration") or info.get("format", {}).get("duration")
    if dur is not None:
        want = frames / float(fps)
        if abs(float(dur) - want) >= 0.05:
            notes.append(f"duration={float(dur):.3f} vs {want:.3f}")
    else:
        notes.append("duration unavailable")
    if expect_audio:
        if not astreams:
            notes.append("audio stream missing")
        else:
            mv = mean_volume(ffmpeg, mp4)
            if mv is None:
                notes.append("audio volume unmeasurable")
            elif mv <= -90.0:
                if silent_source:
                    source_silent_hit = True
                else:
                    notes.append(f"audio silent mean={mv}dB")
    return notes, source_silent_hit, stall_hit

# ---------------------------------------------------------------- subcommands

def cmd_probe(a):
    h = read_header(a.input)
    errs = validate_header(h)
    out = {"frames": h["frames"], "w": h["w"], "h": h["h"], "fps": h["fps"],
           "codec": "KB2" + h["codec"], "size": h["fsize"]}
    if errs:
        out["note"] = "header out of range: " + "; ".join(errs)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not errs else 1


def do_convert(ffmpeg, ffprobe, bk2, out_mp4, crf, no_audio, no_verify, timeout):
    """One conversion job. Returns dict for JSON Lines."""
    t0 = time.time()
    rec = {"file": str(bk2), "status": "failed", "dur_s": 0.0, "frames": 0,
           "audio": False, "vsize": 0, "note": ""}
    try:
        h = read_header(bk2)
    except SystemExit:
        rec["note"] = "bad header"
        rec["dur_s"] = round(time.time() - t0, 2)
        return rec
    errs = validate_header(h)
    rec["frames"] = h["frames"]
    if errs:
        rec["note"] = "invalid header: " + "; ".join(errs)
        rec["dur_s"] = round(time.time() - t0, 2)
        return rec
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    wav = out_mp4.with_suffix(".wav") if not no_audio else None
    # Odd header dims: I420/libx264 need even W/H. The decoder frame buffer is
    # 16-aligned and its padding holds edge-replicated pixels, so request a
    # +1 px even crop and encode at that size (references/pipeline.md #11).
    cw = h["w"] + (h["w"] & 1)
    ch = h["h"] + (h["h"] & 1)
    pad_note = ""
    if (cw, ch) != (h["w"], h["h"]):
        pad_note = f"odd-source padded to {cw}x{ch} (edge-replicated)"
    attempts = 0
    silent_source = False
    while attempts < 2:
        attempts += 1
        rec["note"] = ""
        has_audio = False
        silent_source = False
        if wav:
            nt, db, aok, aerr = decode_audio(bk2, h["frames"], wav, timeout)
            has_audio = nt > 0 and db > 0 and aok > 0
            if has_audio:
                # probe the on-disk pre-mux PCM so verify can downgrade
                # contract ③ for sources whose audio is silent from the start
                silent_source = wav_peak_abs(wav) == 0
            if aerr and not has_audio:
                rec["note"] = aerr
        okf, verr, vnote = pipe_video(bk2, h["frames"], cw, ch, h["fps"],
                                      out_mp4, crf, wav if has_audio else None,
                                      ffmpeg, timeout)
        rec["audio"] = has_audio
        rec["vsize"] = out_mp4.stat().st_size if out_mp4.exists() else 0
        if (verr or okf == 0) and attempts < 2:
            continue  # pitfall: transient hang — retry once
        break
    wav.unlink(missing_ok=True) if wav else None
    if verr or okf == 0 or rec["vsize"] == 0:
        rec["status"] = "failed"
        rec["note"] = (rec["note"] + "; " if rec["note"] else "") + (verr or "no frames produced")
    elif no_verify:
        rec["status"] = "ok"
        rec["note"] = "; ".join([x for x in ("verify skipped", pad_note) if x])
    else:
        vnotes, src_silent, stall = verify(ffmpeg, ffprobe, out_mp4, h["frames"],
                                           h["fps"], rec["audio"], silent_source)
        if vnotes:
            rec["status"] = "flawed"
            if pad_note:
                vnotes.append(pad_note)
            rec["note"] = "; ".join(vnotes)
        else:
            rec["status"] = "ok"
            extras = []
            if not rec["audio"] and not no_audio:
                extras.append("no audio track detected")
            if src_silent:
                extras.append("source-audio-silent (raw PCM peak=0)")
            if vnote:
                extras.append(vnote)
            if pad_note:
                extras.append(pad_note)
            if stall:
                extras.append("last-frame-stall (vendor decoder skipped final frame)")
            rec["note"] = "; ".join(extras)
    rec["dur_s"] = round(time.time() - t0, 2)
    return rec


def cmd_convert(a):
    ffmpeg, ffprobe = need_tools()
    timeout = a.timeout or max(120.0, read_header(a.input)["frames"] * 0.5)
    rec = do_convert(ffmpeg, ffprobe, a.input, a.out, a.crf, a.no_audio,
                     a.no_verify, timeout)
    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["status"] == "ok" else 1


def codec_ok(codec_char, only):
    if only == "all":
        return True
    return ("KB2" + codec_char) == only


def cmd_batch(a):
    ffmpeg, ffprobe = need_tools()
    src = Path(a.srcdir)
    files = sorted([p for p in src.iterdir() if p.suffix.lower() == ".bk2"],
                   key=lambda p: p.name)
    if not files:
        die(f"no .bk2 files under {src}", 1)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    done = 0
    for f in files:
        if a.limit and done >= a.limit:
            break
        out = outdir / (f.stem + ".mp4")
        if a.resume and out.exists() and out.stat().st_size > 0:
            rec = {"file": str(f), "status": "skipped_resume", "dur_s": 0.0,
                   "frames": 0, "audio": None, "vsize": out.stat().st_size,
                   "note": "output exists"}
            print(json.dumps(rec, ensure_ascii=False))
            done += 1
            continue
        try:
            h = read_header(f)
        except SystemExit:
            rec = {"file": str(f), "status": "failed", "dur_s": 0.0, "frames": 0,
                   "audio": False, "vsize": 0, "note": "bad header"}
            print(json.dumps(rec, ensure_ascii=False))
            done += 1
            continue
        if not codec_ok(h["codec"], a.only):
            rec = {"file": str(f), "status": "skipped_codec", "dur_s": 0.0,
                   "frames": h["frames"], "audio": None, "vsize": 0,
                   "note": f"KB2{h['codec']} not in --only {a.only}"}
            print(json.dumps(rec, ensure_ascii=False))
            done += 1
            continue
        timeout = a.timeout or max(120.0, h["frames"] * 0.5)
        rec = do_convert(ffmpeg, ffprobe, f, out, a.crf, a.no_audio, False, timeout)
        if rec["frames"] == 0:
            rec["frames"] = h["frames"]
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        done += 1
    return 0


def extract_one(rpf, outdir, only, limit, jline):
    """Scan one RPF for KB2* magic at 4096-aligned offsets; carve accepted spans.

    Two passes: collect spans while streaming, then write them (keeps the
    streaming file position clean on huge archives).
    """
    rpf = Path(rpf)
    fsize = rpf.stat().st_size
    CHUNK = 8 * 1024 * 1024  # multiple of 4096 -> aligned candidates never straddle
    spans = []  # (offset, declared_size, frames, w, h, fps, codec)
    prev_end = 0  # dedupe: skip hits inside a previously accepted span
    t0 = time.time()
    with open(rpf, "rb") as f:
        pos = 0
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            n = len(buf)
            i = 0
            while i + 4 <= n:  # i is 4096-aligned, pos is too -> p aligned
                p = pos + i
                if p >= prev_end and buf[i:i + 3] == b"KB2":
                    hdr = buf[i:i + 36]
                    if len(hdr) < 36:
                        cur = f.tell()
                        f.seek(p)
                        hdr = f.read(36)
                        f.seek(cur)
                    if len(hdr) == 36:
                        v = struct.unpack(HDR_FMT, hdr[4:36])
                        declared, frames, w, h, fps = v[0], v[1], v[4], v[5], v[6]
                        codec = hdr[3:4].decode("latin1")
                        if (0 < declared <= MAX_SCAN_SLICE and p + declared <= fsize
                                and 0 < frames <= 4_000_000
                                and 0 < w <= 7680 and 0 < h <= 4320
                                and 1 <= fps <= 240
                                and codec_ok(codec, only)):
                            spans.append((p, declared, frames, w, h, fps, codec))
                            prev_end = p + declared
                            if limit and len(spans) >= limit:
                                break
                i += 4096
            pos += n
            if limit and len(spans) >= limit:
                break
    found = 0
    with open(rpf, "rb") as f:
        for (p, declared, frames, w, h, fps, codec) in spans:
            name = f"KB2{codec}_{p}_{w}x{h}_{fps}fps_{frames}f.bk2"
            dst = outdir / name
            f.seek(p)
            with open(dst, "wb") as g:
                rem = declared
                while rem > 0:
                    b = f.read(min(1 << 20, rem))
                    if not b:
                        break
                    g.write(b)
                    rem -= len(b)
            found += 1
            jline({"file": str(dst), "status": "extracted",
                   "dur_s": round(time.time() - t0, 2), "frames": frames,
                   "audio": None, "vsize": declared,
                   "note": f"{rpf.name}@{p} KB2{codec} {w}x{h}@{fps}"})
    return found


def cmd_extract(a):
    src = Path(a.input)
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        rpfs = sorted([p for p in src.iterdir() if p.suffix.lower() == ".rpf"],
                      key=lambda p: p.name)
    else:
        rpfs = [src]
    if not rpfs:
        die(f"no .rpf files at {src}", 1)
    total = 0
    def jline(rec):
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    for r in rpfs:
        t0 = time.time()
        n = extract_one(r, outdir, a.only, a.limit, jline)
        total += n
        jline({"file": str(r), "status": "done" if n else "empty", "dur_s":
               round(time.time() - t0, 2), "frames": n, "audio": None,
               "vsize": r.stat().st_size, "note": f"{n} slices"})
    return 0 if total else 1


def cmd_status(a):
    outdir = Path(a.outdir)
    if not outdir.is_dir():
        die(f"no such directory: {outdir}", 1)
    mp4s = [p for p in outdir.iterdir() if p.suffix.lower() == ".mp4"]
    bk2s = [p for p in outdir.iterdir() if p.suffix.lower() == ".bk2"]
    total = sum(p.stat().st_size for p in mp4s)
    print(json.dumps({
        "outdir": str(outdir), "mp4_count": len(mp4s),
        "mp4_total_bytes": total,
        "mp4_avg_bytes": (total // len(mp4s)) if mp4s else 0,
        "bk2_count": len(bk2s),
        "mp4s": [{"file": p.name, "bytes": p.stat().st_size} for p in sorted(mp4s, key=lambda x: x.name)],
    }, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="bink2mp4.py",
        description="Bink2 .bk2 -> H.264 MP4 converter / RAGE RPF extractor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="read bk2 header -> JSON")
    p.add_argument("input")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("convert", help="one bk2 -> mp4 with auto-verify")
    p.add_argument("input")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--timeout", type=float, default=None,
                   help="per-decode-phase timeout seconds (default max(120, frames*0.5))")
    p.set_defaults(fn=cmd_convert)

    p = sub.add_parser("batch", help="convert every .bk2 in a directory")
    p.add_argument("srcdir")
    p.add_argument("-o", "--outdir", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--only", choices=["KB2n", "KB2j", "all"], default="all")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("extract", help="carve .bk2 streams from .rpf file/dir")
    p.add_argument("input")
    p.add_argument("-o", "--outdir", required=True)
    p.add_argument("--only", choices=["KB2n", "KB2j", "all"], default="all")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after N slices (bounds scan time on huge archives)")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("status", help="summary JSON for an output directory")
    p.add_argument("outdir")
    p.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
