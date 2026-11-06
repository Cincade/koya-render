#!/usr/bin/env python3
"""footage_shard.py <manifest.json> <shard> <shards> <out_dir>  (runs on the farm; 4 Nov 2026)

Composites one block of frames of a talking-head + PiP video with the bundled, unmodified pip.py (TRN.pip-shrink.v2).
The block is split again into `workers` sub-ranges rendered side by side (pip.py is single-threaded numpy). Each
sub-range is a silent x264 piece (pip.py's own encode); the join job concatenates the pieces and adds the voice.
Prints counts only (public log). The face track is computed on the Mac (Apple Vision) and must be in the bundle.
"""
import json, multiprocessing as mp, os, pathlib, sys

man_p, shard, shards, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), pathlib.Path(sys.argv[4])
W = pathlib.Path(man_p).parent
m = json.load(open(man_p))
total = int(m["frames"]); workers = max(1, int(m.get("workers") or 3))
lo, hi = total * shard // shards, total * (shard + 1) // shards
subs = [(lo + (hi - lo) * k // workers, lo + (hi - lo) * (k + 1) // workers) for k in range(workers)]
subs = [s for s in subs if s[1] > s[0]]
tl_path = W / m["timeline"]
sys.path.insert(0, str(tl_path.parent))


def one(rng):
    import pip as P
    tl = P.Timeline.load(str(tl_path)); tl.d["audio"] = None
    if not list((tl_path.parent / "out").glob("track_*.jsonl")): raise SystemExit("face track missing from bundle")
    piece = out / f"piece_{rng[0]:07d}.mp4"
    P.render(tl, str(piece), rng)
    return rng


if __name__ == "__main__":
    out.mkdir(parents=True, exist_ok=True)
    with mp.Pool(len(subs)) as pool:
        for r in pool.imap_unordered(one, subs): print(f"sub-range of {r[1] - r[0]} frames done", flush=True)
    json.dump(dict(shard=shard, lo=lo, hi=hi, pieces=[f"piece_{a:07d}.mp4" for a, _ in subs], fps=m["fps"], total=total),
              open(out / f"meta_{shard:03d}.json", "w"))
