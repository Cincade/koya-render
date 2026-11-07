#!/usr/bin/env python3
"""recap_shard.py <manifest.json> <shard> <shards> <out_dir>  (all-cloud recap, 7 Nov 2026)

ONE job does a whole block of the final video: the board frames AND the PiP composite, so the board never travels
(today the boards came down to the Mac and the composite ran there, or the board went back up: 120 MB, 16 min).

  1. this shard's block of final frames [lo, hi), split on frames whose time pip.py's "-ss {a/fps:.4f}" writes exactly
     (30 fps: multiples of 3 frames; same rule as footage_shard.py, 9bce9ce)
  2. the board page frames those final frames show, from the Mac's board map (one entry per final frame: page frame,
     or -1 where pip.py passes the talking head through and never looks at the board). Rendered exactly like
     render_shard.py (Playwright Chromium, srgb, no LCD text, device scale, jpeg q95), downscaled Lanczos into one
     lossless ffv1 board clip for the block
  3. the bundled, unmodified pip.py (fingerprint checked against the locked hash below) composites sub-ranges of the
     block side by side, reading the speaker cut and the local board clip (an ffconcat that puts the clip at its real time)
  4. a WhatsApp-size encode of the block (the join only stream-copies)
Prints counts and timings only (public log).
"""
import hashlib, json, math, multiprocessing as mp, os, pathlib, shutil, subprocess, sys, time

# playbook/locked.json "research/whiteboard-looks/pip.py" (TRN.pip-shrink.v2, approved by Ife 3 Nov). Any other pip.py is refused.
PIP_SHA = {"4698018d031e5ef6914a227e845da5a9db7260f22cf1f293255cc068c903d542"}

man_p, shard, shards, out = pathlib.Path(sys.argv[1]).resolve(), int(sys.argv[2]), int(sys.argv[3]), pathlib.Path(sys.argv[4]).resolve()
W = man_p.parent
M = json.loads(man_p.read_text())
fps = int(M["fps"]); total = int(M["frames"]); scale = float(M.get("scale") or 2)
VW, VH = (int(v) for v in (M.get("size") or "1920x1080").split("x"))
pip_workers = max(1, int(M.get("pip_workers") or 3))
board_workers = max(1, int(M.get("board_workers") or os.cpu_count() or 2))
ARGS = ["--force-color-profile=srgb", "--disable-lcd-text"] + (M.get("extra_args") or "").split()

STEP = fps // math.gcd(fps, 10000)   # 30 fps -> 3: f/fps then has an exact 4-decimal form
al = lambda f: total if f >= total else (f // STEP) * STEP
lo, hi = al(total * shard // shards), al(total * (shard + 1) // shards)
subs = [(al(lo + (hi - lo) * k // pip_workers) if k else lo, al(lo + (hi - lo) * (k + 1) // pip_workers) if k < pip_workers - 1 else hi)
        for k in range(pip_workers)]
subs = [s for s in subs if s[1] > s[0]]

PIPD = W / pathlib.Path(M["timeline"]).parent
sys.path.insert(0, str(PIPD))   # module level: the bundled pip.py, never the pip package (spawned workers re-import this file)
frames_dir = out / "frames"; seq_dir = out / "seq"


def board_map():
    """run-length [[f0, f1, p0, step]] -> one page frame per final frame (-1 = not needed)."""
    m = [-1] * total
    for f0, f1, p0, st in M["board_map"]:
        for f in range(f0, f1): m[f] = p0 + (f - f0) * st if p0 >= 0 else -1
    return m


def open_page(p, page):
    for attempt in range(3):   # same as render_shard.py: Linux runners sometimes lose the page while it opens
        b = p.chromium.launch(args=ARGS)
        pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=scale)
        pg.set_default_timeout(300000)
        try:
            pg.goto(f"file://{page}?still=1")
            pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready.then(() => 1)")
            pg.wait_for_function("typeof render === 'function'")
            pg.evaluate("Promise.all([...document.images].map(i => i.decode ? i.decode().catch(() => 0) : 0))")
            return b, pg
        except Exception as e:
            print(f"open_page attempt {attempt + 1} failed: {str(e).splitlines()[0]}", flush=True)
            b.close()
            if attempt == 2: raise
            time.sleep(3)


def render_frames(page, todo, k, n):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b, pg = open_page(p, page)
        for i in todo[k::n]:
            pg.evaluate(f"render({i / fps})")
            pg.screenshot(path=str(frames_dir / f"{i:05d}.jpg"), type="jpeg", quality=95)
        b.close()


def one(rng):
    import pip as P
    tl = P.Timeline.load(str(PIPD / f"timeline_{shard:03d}.json")); tl.d["audio"] = None
    piece = out / f"piece_{rng[0]:07d}.mp4"
    P.render(tl, str(piece), rng)
    return rng


def ff(*a):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)


if __name__ == "__main__":
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True); frames_dir.mkdir(exist_ok=True); seq_dir.mkdir(exist_ok=True)
    meta = dict(shard=shard, lo=lo, hi=hi, fps=fps, total=total, pieces=[f"piece_{a:07d}.mp4" for a, _ in subs], whatsapp=None)
    if hi <= lo:
        (out / f"meta_{shard:03d}.json").write_text(json.dumps(meta)); print("empty shard"); sys.exit(0)
    pip_py = PIPD / "pip.py"
    if hashlib.sha256(pip_py.read_bytes()).hexdigest() not in PIP_SHA: sys.exit("pip.py is not the locked, approved version: refused")
    if not list((PIPD / "out").glob("track_*.jsonl")): sys.exit("face track missing from bundle")
    tl0 = json.loads((W / M["timeline"]).read_text())
    spk = PIPD / tl0["speaker"]
    foot = pathlib.Path(os.environ.get("FOOTAGE") or "/nonexistent")
    if foot.exists():   # the speaker cut travels on its own (uploaded early, while Claude plans); it must be the one the Mac planned with
        h = hashlib.sha256()
        with open(foot, "rb") as fh:
            while b := fh.read(8 << 20): h.update(b)
        if h.hexdigest() != M["speaker_sha256"]: sys.exit("speaker cut does not match the manifest (uploaded file changed?)")
        spk.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(foot), spk)
    if not spk.exists(): sys.exit("speaker cut missing")
    import pip as P
    T = P.Timeline.load(str(W / M["timeline"]))
    bm = board_map()
    # guard: every frame pip.py composites over the board must have a board frame (else a black board under the PiP)
    need = lambda f: not (T.progress(f / fps)[0] <= 0 and not T.moving(f / fps, 0.5 / fps))
    holes = [f for f in range(lo, hi) if bm[f] < 0 and need(f)]
    if holes: sys.exit(f"board map has no frame for {len(holes)} composited frames (first {holes[0]})")

    # ---- 1. board frames for this block
    todo = sorted({bm[f] for f in range(lo, hi) if bm[f] >= 0})
    if todo:
        page = (W / M["page"]).resolve()
        n = min(board_workers, len(todo))
        ps = [mp.Process(target=render_frames, args=(page, todo, k, n)) for k in range(n)]
        [x.start() for x in ps]; [x.join() for x in ps]
        for attempt in range(3):   # a worker can die (renderer crash): redo what is missing with one worker
            miss = [i for i in todo if not (frames_dir / f"{i:05d}.jpg").exists()]
            if not miss: break
            print(f"re-rendering {len(miss)} missing frames (attempt {attempt + 1})", flush=True)
            render_frames(page, miss, 0, 1)
        miss = [i for i in todo if not (frames_dir / f"{i:05d}.jpg").exists()]
        if miss: sys.exit(f"missing {len(miss)} board frames")
    t1 = time.time()
    # one jpeg per final frame (links), black where pip.py never looks at the board
    from PIL import Image
    blank = frames_dir / "blank.jpg"; Image.new("RGB", (round(VW * scale), round(VH * scale))).save(blank, quality=95)
    for f in range(lo, hi):
        (seq_dir / f"{f:05d}.jpg").symlink_to(frames_dir / f"{bm[f]:05d}.jpg" if bm[f] >= 0 else blank)
    board = PIPD / f"board_{shard:03d}.nut"   # next to its ffconcat: pip.py opens it without -safe 0, so names stay relative
    ff("-framerate", str(fps), "-start_number", str(lo), "-i", str(seq_dir / "%05d.jpg"), "-frames:v", str(hi - lo),
       "-vf", f"scale={VW}:{VH}:flags=lanczos", "-pix_fmt", "yuv420p", "-c:v", "ffv1", "-level", "3", "-slices", "16", "-g", "1", str(board))
    shutil.rmtree(frames_dir); shutil.rmtree(seq_dir)
    # the clip sits at its real time: pip.py seeks the board to a/fps like a full-length board
    cc = ["ffconcat version 1.0"]
    if lo: cc += [f"file '{board.name}'", "outpoint 0.034", f"duration {lo / fps:.6f}"]
    cc += [f"file '{board.name}'"]
    (PIPD / f"board_{shard:03d}.ffconcat").write_text("\n".join(cc) + "\n")
    (PIPD / f"timeline_{shard:03d}.json").write_text(json.dumps(dict(tl0, board=f"board_{shard:03d}.ffconcat"), indent=1))
    t2 = time.time()

    # ---- 2. PiP composite, sub-ranges side by side (pip.py is single-threaded numpy)
    with mp.Pool(len(subs)) as pool:
        for r in pool.imap_unordered(one, subs): print(f"sub-range of {r[1] - r[0]} frames done", flush=True)
    board.unlink()
    t3 = time.time()

    # ---- 3. WhatsApp-size copy of the block (same encoder settings in every shard, so the join can stream-copy)
    kb = int(M.get("wa_kbps") or 0)
    if kb:
        (out / "pl.txt").write_text("".join(f"file '{out / p}'\n" for p in meta["pieces"]))
        wa = out / f"wa_{shard:03d}.mp4"
        ff("-f", "concat", "-safe", "0", "-i", str(out / "pl.txt"), "-c:v", "libx264", "-b:v", f"{kb}k", "-maxrate", f"{int(kb * 1.15)}k",
           "-bufsize", f"{kb * 2}k", "-preset", "slow", "-pix_fmt", "yuv420p", "-r", str(fps), "-an", str(wa))
        (out / "pl.txt").unlink(); meta["whatsapp"] = wa.name
    meta.update(board_frames=len(todo), board_s=round(t1 - t0), board_clip_s=round(t2 - t1), pip_s=round(t3 - t2), wa_s=round(time.time() - t3))
    (out / f"meta_{shard:03d}.json").write_text(json.dumps(meta))
    print(json.dumps({k: v for k, v in meta.items() if k not in ("pieces",)}), flush=True)
