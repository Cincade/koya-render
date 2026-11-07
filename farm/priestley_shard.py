#!/usr/bin/env python3
"""priestley_shard.py: the Priestley-style PiP mode (rounded-rect card, hard cuts, talking-head creep). 7 Nov 2026.

A NEW compositor next to the locked pip.py (TRN.pip-shrink.v2, untouched). Same farm shape as recap_shard.py: one job
does a block of final frames: board page frames (Playwright Chromium, same flags as render_shard.py, device scale 2,
jpeg q95, Lanczos to 1920x1080), then the composite, then one x264 piece with pip.py's exact encode settings
(libx264 medium crf 17 yuv420p), so farm/run.sh recap_join stream-copies the pieces unchanged.

Ife's footage is never graded, blurred or relit. The only thing ever done to it is a UNIFORM scale (one factor for x
and y, Lanczos):
  th / tt  full frame; the creep scales it about a fixed pivot, 1.00 -> cap at `rate` per second, back to 1.00 at
           every reset (a framing cut). At scale 1.00 the decoded frame passes through untouched.
  pip      the full frame height scaled by 824/1080 into the 516 x 824 rounded card (fixed, hard cut in and out).
  board    no footage.
Talking-head words are bitmaps baked once at 1x (R = letters, G = soft shadow), faded in on their spoken onsets,
held still, faded out over 4 frames; they are the only thing drawn over the footage.

usage (farm):  priestley_shard.py <manifest.json> <shard> <shards> <out_dir>      (env FOOTAGE = the speaker cut)
usage (Mac check, no render): priestley_shard.py --frames <timeline.json> <plate_root> <speaker.mp4> <board_png_dir> <out_dir> f1 f2 ...
"""
import json, math, os, pathlib, shutil, subprocess, sys, time
import numpy as np
from PIL import Image, ImageDraw

W, H = 1920, 1080


# ================================================================ compositor (pure: frame in, frame out)
def sstep(a, b, x):
    k = min(max((x - a) / (b - a), 0.0), 1.0); return k * k * (3 - 2 * k)


class Comp:
    def __init__(self, tl, plate_root):
        self.tl = tl; self.fps = tl["fps"]; self.root = pathlib.Path(plate_root)
        p = tl["pip"]; self.P = p
        ss = 4; m = Image.new("L", (p["w"] * ss, p["h"] * ss), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, p["w"] * ss - 1, p["h"] * ss - 1], p["r"] * ss, fill=255)
        self.mask = np.asarray(m.resize((p["w"], p["h"]), Image.LANCZOS), np.float32) / 255
        self.tt = []
        for g in tl["tt"]:
            ws = []
            for w in g["words"]:
                a = np.asarray(Image.open(self.root / w["src"]).convert("RGBA"), np.float32) / 255
                ws.append(dict(w, R=a[..., 0], G=a[..., 1]))
            self.tt.append(dict(words=ws, out=g["out"]))

    def frame_of(self, t):   # a cut at time c takes effect on the first frame at or after c
        return math.ceil(t * self.fps - 1e-6)

    def mode(self, f):
        for s in self.tl["segs"]:
            if self.frame_of(s["t0"]) <= f < self.frame_of(s["t1"]): return s["mode"]
        return self.tl["segs"][-1]["mode"]

    def scale(self, f):
        c = self.tl["creep"]; r = max(x for x in self.tl["resets"] if self.frame_of(x) <= f)
        n = f - self.frame_of(r)   # frames since the reset (the reset frame itself is 1.00 exactly)
        return min(1.0 + c["rate"] * n / self.fps, c["cap"])

    def needs_board(self, f): return self.mode(f) in ("pip", "board")

    def creep(self, src, s):
        if s == 1.0: return src
        px, py = self.tl["creep"]["pivot"]
        x0, y0 = px - px / s, py - py / s
        return src.resize((W, H), Image.LANCZOS, box=(x0, y0, x0 + W / s, y0 + H / s))

    def pip_card(self, src):
        p = self.P; k = p["h"] / H; cw = p["w"] / k   # uniform: full height -> card height
        x0 = p["crop_cx"] - cw / 2
        return src.resize((p["w"], p["h"]), Image.LANCZOS, box=(x0, 0, x0 + cw, H))

    def words(self, out, f):
        t = f / self.fps; shadow = None; letters = None
        for g in self.tt:
            if not (g["words"][0]["t"] - 0.1 <= t < g["out"] + 0.2): continue
            go = 1 - sstep(g["out"], g["out"] + 4 / self.fps, t)
            for w in g["words"]:
                a = sstep(w["t"] - 0.02, w["t"] + 0.10, t) * go
                if a <= 0.002: continue
                if shadow is None: shadow = np.zeros((H, W), np.float32); letters = np.zeros((H, W), np.float32)
                y0, x0 = w["y"], w["x"]; h_, w_ = w["R"].shape
                sl = (slice(y0, y0 + h_), slice(x0, x0 + w_))
                shadow[sl] = np.maximum(shadow[sl], a * w["G"]); letters[sl] += a * w["R"]
        if shadow is None: return out, None
        letters = np.clip(letters, 0, 1)
        o = np.asarray(out, np.float32)
        o = o * (1 - shadow[..., None]); o = o * (1 - letters[..., None]) + 255 * letters[..., None]
        return Image.fromarray(np.clip(o + 0.5, 0, 255).astype(np.uint8)), (shadow > 0) | (letters > 0)

    def composite(self, f, src, board):
        """src: PIL RGB 1920x1080 speaker frame (as decoded). board: PIL RGB 1920x1080 or None. -> PIL RGB"""
        m = self.mode(f)
        if m == "board": return board
        if m == "pip":
            out = np.asarray(board, np.float32).copy()
            p = self.P; card = np.asarray(self.pip_card(src), np.float32); A = self.mask[..., None]
            sl = (slice(p["y"], p["y"] + p["h"]), slice(p["x"], p["x"] + p["w"]))
            out[sl] = card * A + out[sl] * (1 - A)
            return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))
        out = self.creep(src, self.scale(f))
        return self.words(out, f)[0]


# ================================================================ farm shard
def open_page(p, page, VW, VH, scale, args):
    for attempt in range(3):
        b = p.chromium.launch(args=args)
        pg = b.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=scale)
        pg.set_default_timeout(300000)
        try:
            pg.goto(f"file://{page}?still=1"); pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready.then(() => 1)")
            pg.wait_for_function("typeof render === 'function' && window.READY === true")
            return b, pg
        except Exception as e:
            print(f"open_page attempt {attempt + 1} failed: {str(e).splitlines()[0]}", flush=True); b.close()
            if attempt == 2: raise
            time.sleep(3)


def render_frames(page, todo, k, n, fps, frames_dir, args):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b, pg = open_page(p, page, W, H, 2, args)
        for i in todo[k::n]:
            pg.evaluate(f"render({i / fps})")
            pg.screenshot(path=str(frames_dir / f"{i:05d}.jpg"), type="jpeg", quality=95)
        b.close()


def shard_main(man_p, shard, shards, out):
    import multiprocessing as mp
    t0 = time.time(); Wd = man_p.parent; M = json.loads(man_p.read_text())
    tl = json.loads((Wd / M["timeline"]).read_text()); fps = int(tl["fps"]); total = int(M["frames"])
    STEP = fps // math.gcd(fps, 10000)
    al = lambda f: total if f >= total else (f // STEP) * STEP
    lo, hi = al(total * shard // shards), al(total * (shard + 1) // shards)
    out.mkdir(parents=True, exist_ok=True)
    meta = dict(shard=shard, lo=lo, hi=hi, fps=fps, total=total, pieces=[f"piece_{lo:07d}.mp4"], whatsapp=None)
    if hi <= lo:
        meta["pieces"] = []; (out / f"meta_{shard:03d}.json").write_text(json.dumps(meta)); print("empty shard"); return
    foot = pathlib.Path(os.environ.get("FOOTAGE") or "/nonexistent")
    import hashlib
    h = hashlib.sha256()
    with open(foot, "rb") as fh:
        while b := fh.read(8 << 20): h.update(b)
    if h.hexdigest() != M["speaker_sha256"]: sys.exit("speaker cut does not match the manifest")
    C = Comp(tl, Wd / M["plate_root"])
    todo = [f for f in range(lo, hi) if C.needs_board(f)]
    frames_dir = out / "frames"; frames_dir.mkdir(exist_ok=True)
    args = ["--force-color-profile=srgb", "--disable-lcd-text"]
    if todo:
        page = (Wd / M["page"]).resolve(); n = min(os.cpu_count() or 2, len(todo), 4)
        ps = [mp.Process(target=render_frames, args=(page, todo, k, n, fps, frames_dir, args)) for k in range(n)]
        [x.start() for x in ps]; [x.join() for x in ps]
        for attempt in range(3):
            miss = [i for i in todo if not (frames_dir / f"{i:05d}.jpg").exists()]
            if not miss: break
            print(f"re-rendering {len(miss)} missing frames (attempt {attempt + 1})", flush=True)
            render_frames(page, miss, 0, 1, fps, frames_dir, args)
        miss = [i for i in todo if not (frames_dir / f"{i:05d}.jpg").exists()]
        if miss: sys.exit(f"missing {len(miss)} board frames")
    t1 = time.time()
    fb = W * H * 3
    src = subprocess.Popen(["ffmpeg", "-v", "error", "-threads", "2", "-ss", f"{lo / fps:.4f}", "-t", str((hi - lo) / fps + 0.5), "-i", str(foot),
                            "-vf", f"fps={fps},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    piece = out / meta["pieces"][0]
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
                            "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", str(piece)], stdin=subprocess.PIPE)
    n_done = 0
    for f in range(lo, hi):
        raw = src.stdout.read(fb)
        if len(raw) < fb: sys.exit(f"speaker ended early at frame {f}")
        s_im = Image.frombuffer("RGB", (W, H), raw, "raw", "RGB", 0, 1)
        b_im = Image.open(frames_dir / f"{f:05d}.jpg").convert("RGB").resize((W, H), Image.LANCZOS) if C.needs_board(f) else None
        o = C.composite(f, s_im, b_im)
        enc.stdin.write(o.tobytes()); n_done += 1
    enc.stdin.close(); enc.wait(); src.kill()
    if enc.returncode: sys.exit("encode failed")
    shutil.rmtree(frames_dir)
    meta.update(board_frames=len(todo), board_s=round(t1 - t0), pip_s=round(time.time() - t1), wa_s=0, frames_done=n_done)
    (out / f"meta_{shard:03d}.json").write_text(json.dumps(meta))
    print("sub-range done", json.dumps({k: v for k, v in meta.items() if k != "pieces"}), flush=True)


def frames_main(tlp, root, speaker, board_dir, out_dir, frames):
    """Mac check: composite chosen frames as PNG (no video). Board frames come from page_stills.py PNGs."""
    tl = json.loads(pathlib.Path(tlp).read_text()); C = Comp(tl, root); fps = tl["fps"]
    out_dir = pathlib.Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    for f in frames:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{f / fps:.4f}", "-i", str(speaker), "-frames:v", "1", "-vf", f"fps={fps},scale={W}:{H}",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        s_im = Image.frombuffer("RGB", (W, H), raw, "raw", "RGB", 0, 1).copy(); s_im.save(out_dir / f"src_{f:05d}.png")
        b = pathlib.Path(board_dir) / f"b_{f / fps:07.3f}.png"
        b_im = Image.open(b).convert("RGB") if C.needs_board(f) else None
        C.composite(f, s_im, b_im).save(out_dir / f"c_{f:05d}.png")
        print(f, C.mode(f), round(C.scale(f), 4) if C.mode(f) in ("th",) else "")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--frames": frames_main(a[1], a[2], a[3], a[4], a[5], [int(x) for x in a[6:]])
    else: shard_main(pathlib.Path(a[0]).resolve(), int(a[1]), int(a[2]), pathlib.Path(a[3]).resolve())
