#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Full-corpus reliability sweep driver for the bink2mp4 skill.

stdlib-only. Run with: python -I sweep.py <stageA|stageB|all|status> --corpus DIR --out DIR [--resume]

stageA: decode-stream verification of every bk2 (binkdec_raw -> ffmpeg signalstats -> null),
        zero media written to disk. Appends out/sweep_stageA.csv, rounds to out/logs/rounds.log.
stageB: stratified full-contract verification via the skill's convert subcommand.
        Appends out/sweep_stageB.csv, thumbs to out/thumbs, staging mp4s deleted per file.
status: aggregate JSON of both CSVs.
"""
import argparse
import csv
import os
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(os.environ.get("BINK2_SKILL_DIR", ""))  # set to your skill install dir
BIN_DIR = SKILL_DIR / "assets" / "bin"
EXE = BIN_DIR / "binkdec_raw.exe"
ENTRY = SKILL_DIR / "scripts" / "bink2mp4.py"
FFMPEG = "ffmpeg"

DEFAULT_CORPUS = Path(os.environ.get("BINK2_CORPUS", ""))  # set to your bk2 corpus dir
DEFAULT_OUT = Path(os.environ.get("BINK2_OUT", ""))  # set to your output dir

STAGEA_HEADER = ["id", "fourcc", "w", "h", "frames", "decoded", "status",
                 "yavg", "uavg", "vavg", "satavg", "rc1", "rc2", "secs"]
STAGEB_HEADER = ["id", "class", "status", "dur", "frames", "audio",
                 "mean_volume", "note", "thumbs"]

ROUND_BYTES = 2 * 1024 ** 3
STAGING_CAP = 4 * 1024 ** 3


# ---------------------------------------------------------------- corpus

def read_headers(corpus):
    """bink2_headers.csv -> list of dicts (id, path, codec, size, frames, w, h, fps)."""
    p = Path(corpus) / "bink2_headers.csv"
    recs = []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            recs.append({
                "id": Path(row["file"]).stem,
                "path": str(Path(corpus) / row["file"]),
                "codec": row["codec"].strip(),
                "size": int(row["size"]),
                "frames": int(row["frames"]),
                "w": int(row["w"]),
                "h": int(row["h"]),
                "fps": row["fps"].strip(),
            })
    return recs


def read_csv_ids(path):
    p = Path(path)
    if not p.exists():
        return set()
    with open(p, "r", encoding="utf-8", newline="") as f:
        return {r["id"] for r in csv.DictReader(f) if r.get("id")}


def read_csv_rows(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_row(path, header, row):
    p = Path(path)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, "a", encoding="utf-8", newline="") as f:
        wtr = csv.writer(f)
        if new:
            wtr.writerow(header)
        wtr.writerow(row)
        f.flush()


# ---------------------------------------------------------------- stageA

def parse_signalstats(stats_path):
    """-> (decoded, yavg3, uavg, vavg, satavg). yavg3 = mean of first/mid/last YAVG."""
    vals = {"YAVG": [], "UAVG": [], "VAVG": [], "SATAVG": []}
    try:
        with open(stats_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"lavfi\.signalstats\.(YAVG|UAVG|VAVG|SATAVG)=(-?[\d.]+)", line)
                if m:
                    vals[m.group(1)].append(float(m.group(2)))
    except OSError:
        pass
    y = vals["YAVG"]
    decoded = len(y)
    yavg3 = ""
    if y:
        pts = [y[0], y[len(y) // 2], y[-1]]
        yavg3 = f"{sum(pts) / len(pts):.2f}"

    def mean(k):
        v = vals[k]
        return f"{sum(v) / len(v):.2f}" if v else ""

    return decoded, yavg3, mean("UAVG"), mean("VAVG"), mean("SATAVG")


def stagea_attempt(rec, out, timeout):
    """One decode+signalstats pass. Returns (row_dict, secs)."""
    w, h = rec["w"], rec["h"]
    we, he = w + (w & 1), h + (h & 1)  # even-ize, edge-replicated padding semantics
    logs = Path(out) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stats_name = f"stats_{rec['id']}.txt"
    stats = logs / stats_name
    fflog = logs / f"stageA_{rec['id']}.ffmpeg.log"
    stats.unlink(missing_ok=True)
    # ffmpeg filtergraph parsing chokes on Windows paths (backslashes, drive
    # colon) -> run ffmpeg with cwd=logs and pass a bare relative filename.
    cmd_ff = [FFMPEG, "-hide_banner", "-loglevel", "error",
              "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{we}x{he}",
              "-r", str(rec["fps"]), "-i", "-",
              "-vf", f"signalstats,metadata=print:file={stats_name}",
              "-f", "null", "-"]
    cmd_ex = [str(EXE), str(Path(rec["path"]).resolve()), str(rec["frames"]),
              ".", "v", "raw", f"crop:{we}x{he}"]
    t0 = time.time()
    rc1 = rc2 = ""
    ff = ex = None
    try:
        with open(fflog, "wb") as lf:
            ff = subprocess.Popen(cmd_ff, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=lf,
                                  cwd=str(logs))
            ex = subprocess.Popen(cmd_ex, cwd=str(BIN_DIR), stdout=ff.stdin,
                                  stderr=lf)
            ff.stdin.close()
            try:
                rc1 = ex.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                ex.kill()
                try:
                    ex.wait(10)
                except subprocess.TimeoutExpired:
                    pass
                rc1 = "TO"
            try:
                rc2 = ff.wait(timeout=120)
            except subprocess.TimeoutExpired:
                ff.kill()
                rc2 = "TO"
            ff = ex = None
    finally:
        for p in (ex, ff):
            if p is not None:
                try:
                    p.kill()
                except OSError:
                    pass
    secs = round(time.time() - t0, 2)
    decoded, yavg, uavg, vavg, satavg = parse_signalstats(stats)
    frames = rec["frames"]
    if decoded == frames and rc1 == 0 and rc2 == 0:
        status = "ok"
    elif (rc1 == "TO" or decoded < frames) and rec["codec"] == "KB2j" and w >= 1920:
        status = "known-flawed"  # known KB2j end-frame hang
    else:
        status = "failed"
    if status == "ok":
        stats.unlink(missing_ok=True)
        fflog.unlink(missing_ok=True)
    row = [rec["id"], "KB2" + rec["codec"] if not rec["codec"].startswith("KB2") else rec["codec"],
           w, h, frames, decoded, status, yavg, uavg, vavg, satavg, rc1, rc2, secs]
    return row, secs, status


def cmd_stageA(args):
    corpus, out = Path(args.corpus), Path(args.out)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    recs = read_headers(corpus)
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        recs = [r for r in recs if r["id"] in want]
    csv_path = out / "sweep_stageA.csv"
    done = read_csv_ids(csv_path) if args.resume else set()
    recs = [r for r in recs if r["id"] not in done]
    if args.limit:
        recs = recs[: args.limit]
    rounds_log = out / "logs" / "rounds.log"
    # resume accounting: rebuild cumulative counters from existing rows
    prior = read_csv_rows(csv_path) if args.resume else []
    prior_ids = {r["id"] for r in prior}
    cum_bytes = sum(r["size"] for r in recs_all_lookup(corpus, prior_ids)) if prior else 0
    counts = {"ok": 0, "failed": 0, "known-flawed": 0}
    for r in prior:
        if r["status"] in counts:
            counts[r["status"]] += 1
    n_files = len(prior)
    next_round = (cum_bytes // ROUND_BYTES) + 1
    t_start = time.time()
    new_rounds_log = not rounds_log.exists()
    for i, rec in enumerate(recs, 1):
        row, secs, status = stagea_attempt(rec, out, args.timeout)
        if status == "failed":  # retry once
            row2, secs2, status2 = stagea_attempt(rec, out, args.timeout)
            row, secs, status = row2, round(secs + secs2, 2), status2
            row[13] = secs
        append_row(csv_path, STAGEA_HEADER, row)
        counts[status] = counts.get(status, 0) + 1
        n_files += 1
        cum_bytes += rec["size"]
        print(f"[stageA {i}/{len(recs)}] {rec['id']} {status} "
              f"{row[5]}/{row[4]} {secs}s", flush=True)
        if cum_bytes >= next_round * ROUND_BYTES:
            with open(rounds_log, "a", encoding="utf-8") as f:
                if new_rounds_log:
                    f.write("round,files,src_gb,ok,failed,known_flawed,wall_s\n")
                    new_rounds_log = False
                f.write(f"{next_round},{n_files},{cum_bytes / 1024 ** 3:.3f},"
                        f"{counts['ok']},{counts['failed']},{counts['known-flawed']},"
                        f"{round(time.time() - t_start, 1)}\n")
            next_round += 1
    print(json.dumps({"stage": "A", "processed": len(recs), "counts": counts,
                      "cum_src_gb": round(cum_bytes / 1024 ** 3, 3),
                      "wall_s": round(time.time() - t_start, 1)}))


def recs_all_lookup(corpus, ids):
    if not ids:
        return []
    return [r for r in read_headers(corpus) if r["id"] in ids]


# ---------------------------------------------------------------- stageB

def dir_size(p):
    total = 0
    for f in Path(p).iterdir():
        if f.is_file():
            total += f.stat().st_size
    return total


def enforce_staging_cap(staging):
    while dir_size(staging) > STAGING_CAP:
        files = sorted((f for f in Path(staging).iterdir() if f.is_file()),
                       key=lambda f: f.stat().st_mtime)
        if not files:
            break
        files[0].unlink(missing_ok=True)


def make_thumb(mp4, frame_idx, out_jpg):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp4),
           "-vf", f"select=eq(n\\,{frame_idx}),scale=480:-2",
           "-frames:v", "1", "-q:v", "3", str(out_jpg)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        return r.returncode == 0 and Path(out_jpg).exists()
    except subprocess.TimeoutExpired:
        return False


def select_stageB(recs, stagea_rows, seed=20261124, n_random=15):
    """Stratified selection: 1 per (w,h,fourcc) class (prefer stageA ok)
    + all stageA failed/known-flawed + n_random random, deduped, order kept."""
    a_by_id = {r["id"]: r for r in stagea_rows}
    by_id = {r["id"]: r for r in recs}
    chosen = []

    def add(rid):
        if rid in by_id and rid not in chosen:
            chosen.append(rid)

    classes = {}
    for r in recs:
        key = (r["w"], r["h"], r["codec"])
        classes.setdefault(key, []).append(r["id"])
    for key in sorted(classes):
        ids = classes[key]
        ok = [i for i in ids if a_by_id.get(i, {}).get("status") == "ok"]
        add(ok[0] if ok else ids[0])
    for rid, a in a_by_id.items():
        if a.get("status") in ("failed", "known-flawed"):
            add(rid)
    rnd = random.Random(seed)
    for rid in rnd.sample([r["id"] for r in recs], min(n_random, len(recs))):
        add(rid)
    return chosen


def cmd_stageB(args):
    corpus, out = Path(args.corpus), Path(args.out)
    staging = out / "staging"
    thumbs = out / "thumbs"
    (out / "logs").mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    thumbs.mkdir(parents=True, exist_ok=True)
    recs = read_headers(corpus)
    by_id = {r["id"]: r for r in recs}
    stagea_rows = read_csv_rows(out / "sweep_stageA.csv")
    if args.ids:
        want = [s.strip() for s in args.ids.split(",") if s.strip()]
        sel = [i for i in want if i in by_id]
    else:
        sel = select_stageB(recs, stagea_rows)
    csv_path = out / "sweep_stageB.csv"
    done = read_csv_ids(csv_path) if args.resume else set()
    sel = [i for i in sel if i not in done]
    if args.limit:
        sel = sel[: args.limit]
    t_start = time.time()
    for i, rid in enumerate(sel, 1):
        rec = by_id[rid]
        cls = f"{rec['w']}x{rec['h']}:{rec['codec']}"
        mp4 = staging / f"{rid}.mp4"
        cmd = [sys.executable, "-I", str(ENTRY), "convert", rec["path"],
               "-o", str(mp4)]
        status, dur, frames, audio, note = "failed", "", str(rec["frames"]), "", ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=args.timeout)
            jline = ""
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    jline = line
            rec_j = json.loads(jline) if jline else {}
            status = rec_j.get("status", f"failed(rc={r.returncode})")
            dur = rec_j.get("dur_s", "")
            frames = rec_j.get("frames", rec["frames"])
            audio = rec_j.get("audio", "")
            note = (rec_j.get("note", "") or "").replace(",", ";")
        except subprocess.TimeoutExpired:
            note = "convert timeout"
        except json.JSONDecodeError:
            note = "convert json parse error"
        mean_volume = ""
        m = re.search(r"mean=(-?[\d.]+)\s*dB", str(note))
        if m:
            mean_volume = m.group(1)
        thumb_names = []
        if mp4.exists() and status in ("ok", "flawed"):
            nf = int(frames) if str(frames).isdigit() else rec["frames"]
            f10 = min(10, max(0, nf - 1))
            fmid = nf // 2
            for idx, tag in ((f10, "f10"), (fmid, "mid")):
                jpg = thumbs / f"{rid}_{tag}.jpg"
                if make_thumb(mp4, idx, jpg):
                    thumb_names.append(tag)
        mp4.unlink(missing_ok=True)
        enforce_staging_cap(staging)
        row = [rid, cls, status, dur, frames, audio, mean_volume, note,
               ";".join(thumb_names)]
        append_row(csv_path, STAGEB_HEADER, row)
        print(f"[stageB {i}/{len(sel)}] {rid} {status} {note}", flush=True)
    print(json.dumps({"stage": "B", "processed": len(sel),
                      "wall_s": round(time.time() - t_start, 1)}))


# ---------------------------------------------------------------- status

def cmd_status(args):
    out = Path(args.out)
    corpus = Path(args.corpus)
    a_rows = read_csv_rows(out / "sweep_stageA.csv")
    b_rows = read_csv_rows(out / "sweep_stageB.csv")
    sizes = {r["id"]: r["size"] for r in read_headers(corpus)}
    a_counts = {}
    for r in a_rows:
        a_counts[r["status"]] = a_counts.get(r["status"], 0) + 1
    b_counts = {}
    for r in b_rows:
        b_counts[r["status"]] = b_counts.get(r["status"], 0) + 1
    total = sum(r["frames"] for r in read_headers(corpus))

    def secs(rows, key):
        s = 0.0
        for r in rows:
            try:
                s += float(r.get(key, "") or 0)
            except ValueError:
                pass
        return round(s, 1)

    out_json = {
        "stageA": {
            "rows": len(a_rows), "counts": a_counts,
            "anomalies": [r["id"] for r in a_rows if r["status"] != "ok"],
            "src_gb": round(sum(sizes.get(r["id"], 0) for r in a_rows) / 1024 ** 3, 3),
            "secs": secs(a_rows, "secs"),
        },
        "stageB": {
            "rows": len(b_rows), "counts": b_counts,
            "anomalies": [r["id"] for r in b_rows if r["status"] != "ok"],
            "secs": secs(b_rows, "dur"),
        },
        "corpus": {"files": len(sizes), "sum_frames": total,
                   "src_gb": round(sum(sizes.values()) / 1024 ** 3, 3)},
    }
    print(json.dumps(out_json, ensure_ascii=False))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stageA", "stageB", "all", "status"):
        p = sub.add_parser(name)
        p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
        p.add_argument("--out", default=str(DEFAULT_OUT))
        p.add_argument("--resume", action="store_true")
        p.add_argument("--ids", default="",
                       help="comma-separated id whitelist (smoke runs)")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--timeout", type=float,
                       default=300.0 if name in ("stageA", "all") else 1200.0)
    a = ap.parse_args()
    if a.cmd == "stageA":
        cmd_stageA(a)
    elif a.cmd == "stageB":
        cmd_stageB(a)
    elif a.cmd == "all":
        cmd_stageA(a)
        a.timeout = 1200.0
        cmd_stageB(a)
    elif a.cmd == "status":
        cmd_status(a)


if __name__ == "__main__":
    sys.exit(main())
