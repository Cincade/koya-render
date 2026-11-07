#!/usr/bin/env bash
# Farm steps. Logs stay neutral: no file names, page paths or content are printed (the repo is public).
# The encryption key arrives as env RENDER_KEY (Actions secret) and is passed to openssl via an env reference, never echoed.
set -euo pipefail
RENDER_KEY="$(printf %s "${RENDER_KEY:-}" | tr -d '\r\n')"; export RENDER_KEY   # the secret may carry a trailing newline
FF_DIR="$HOME/ffmpeg"
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:RENDER_KEY "$@"; }

ffmpeg_tool() {
  if [ ! -x "$FF_DIR/bin/ffmpeg" ]; then
    curl -sL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" | tar xJ
    mv "${FFMPEG_BUILD}" "$FF_DIR"
  fi
  echo "$FF_DIR/bin" >> "$GITHUB_PATH"
}

case "$1" in
  tools)
    pip install -q "playwright==${PLAYWRIGHT_VERSION}"
    python -m playwright install chromium-headless-shell >/dev/null
    python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); p.chromium.launch().close(); p.stop()" \
      || sudo python -m playwright install-deps chromium-headless-shell >/dev/null
    ffmpeg_tool ;;
  ffmpeg)
    ffmpeg_tool ;;
  shard)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; mkdir -p "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/enc"
    # 30 Oct: the scene assembler (tools/scenemaker/assemble) uploads big bundles as small parts bundle.part_000, ...
    # so a slow link retries piece by piece; join them back. Single-file jobs (cloud_render.py) are unchanged.
    B=bundle.tar.gz.enc
    if [ ! -f "$B" ] && ls bundle.part_* >/dev/null 2>&1; then cat bundle.part_* > "$W/b.enc"; B="$W/b.enc"; fi
    enc -d -in "$B" -out "$W/b.tgz"; rm -f "$W/b.enc"
    tar xzf "$W/b.tgz" -C "$W"; rm "$W/b.tgz"
    PAGE=$(python3 -c "import json; print(json.load(open('$W/manifest.json'))['page'])")
    # mask anything derived from the bundle in case a stack trace prints it
    echo "::add-mask::$PAGE"; echo "::add-mask::$(basename "$PAGE")"; echo "::add-mask::$W/$PAGE"
    python3 farm/render_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$RUNNER_TEMP/out"
    tar cf - -C "$RUNNER_TEMP/out" --exclude=frames . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$RUNNER_TEMP/out" ;;
  join)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; mkdir -p "$J" "$RUNNER_TEMP/final"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    cd "$J"
    python3 - <<EOF
import json, glob
m = [json.load(open(f)) for f in sorted(glob.glob("meta_*.json"))]
assert len(m) == int("$SHARDS"), f"got {len(m)} shard metas"
exp = m[0]["total"]; got = sum(x["hi"] - x["lo"] for x in m)
assert got == exp, (got, exp)
open("expected", "w").write(str(exp)); open("fps", "w").write(str(m[0]["fps"]))
open("list.txt", "w").write("".join(f"file 'chunk_{x['shard']:03d}.nut'\n" for x in sorted(m, key=lambda x: x["lo"]) if x["hi"] > x["lo"]))
print("expected frames", exp)
EOF
    FPS=$(cat fps)
    ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -vf "setpts=N/(${FPS}*TB)" -c:v libx264 -pix_fmt yuv420p -crf 16 \
      -preset medium -r "$FPS" -movflags +faststart render.mp4
    got=$(ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of default=nw=1:nk=1 render.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    enc -in render.mp4 -out "$RUNNER_TEMP/final/render.mp4.enc"
    cd /; rm -rf "$J" "$P" ;;
  warm)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    python3 farm/warm_worker.py ;;
  footage_tools)
    # 4 Nov 2026: own-footage PiP composite. pip.py needs numpy, pillow, scipy.
    pip install -q numpy pillow scipy
    ffmpeg_tool ;;
  footage_shard)
    # Owner's own footage only (guarded by the client). Decrypt, composite this shard's frames, encrypt the pieces, delete the plaintext.
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; mkdir -p "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/enc"
    cat bundle.part_* | enc -d -out "$W/b.tar"
    tar xf "$W/b.tar" -C "$W"; rm -f "$W/b.tar"
    if ! python3 farm/footage_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$RUNNER_TEMP/out" > "$RUNNER_TEMP/shard.log" 2>&1; then
      # details only inside an encrypted file (public log): the client can fetch and decrypt it
      tail -c 4000 "$RUNNER_TEMP/shard.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$SHARD").enc"
      rm -rf "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/shard.log"; echo "shard failed"; exit 1
    fi
    echo "sub-ranges done: $(grep -c 'done' "$RUNNER_TEMP/shard.log")"
    tar cf - -C "$RUNNER_TEMP/out" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$RUNNER_TEMP/out" "$RUNNER_TEMP/shard.log" ;;
  footage_join)
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; F="$RUNNER_TEMP/final"; mkdir -p "$J" "$F"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    enc -d -in audio.enc -out "$J/voice.wav"
    cd "$J"
    SHARDS="$SHARDS" python3 -c '
import json, glob, os
m = sorted((json.load(open(f)) for f in glob.glob("meta_*.json")), key=lambda x: x["lo"])
assert len(m) == int(os.environ["SHARDS"]), "shard metas missing"
assert sum(x["hi"] - x["lo"] for x in m) == m[0]["total"]
open("expected", "w").write(str(m[0]["total"])); open("fps", "w").write(str(m[0]["fps"]))
open("list.txt", "w").write("".join("file %s\n" % p for x in m for p in x["pieces"]))
print("pieces", sum(len(x["pieces"]) for x in m))
'
    FPS=$(cat fps)
    ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -c copy silent.mp4
    got=$(ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of default=nw=1:nk=1 silent.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    # final about 9 Mbps and the WhatsApp copy at 1700k (APPROVED Delivery), encoded side by side; voice as AAC
    ffmpeg -y -loglevel error -i silent.mp4 -i voice.wav -map 0:v -map 1:a -c:v libx264 -preset medium -b:v 9M -maxrate 11M -bufsize 18M \
      -pix_fmt yuv420p -r "$FPS" -c:a aac -b:a 192k -shortest -movflags +faststart final.mp4 & P1=$!
    ffmpeg -y -loglevel error -i silent.mp4 -i voice.wav -map 0:v -map 1:a -c:v libx264 -preset medium -b:v 1700k -maxrate 2000k -bufsize 3400k \
      -pix_fmt yuv420p -r "$FPS" -c:a aac -b:a 128k -shortest -movflags +faststart whatsapp.mp4 & P2=$!
    wait $P1; wait $P2
    enc -in final.mp4 -out "$F/final.mp4.enc"; enc -in whatsapp.mp4 -out "$F/whatsapp.mp4.enc"
    cd /; rm -rf "$J" "$P" ;;
  recap_tools)
    # 7 Nov 2026: all-cloud recap = board page (Playwright Chromium) + pip.py (numpy, pillow, scipy) in the same job
    pip install -q "playwright==${PLAYWRIGHT_VERSION}" numpy pillow scipy
    python -m playwright install chromium-headless-shell >/dev/null
    python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); p.chromium.launch().close(); p.stop()" \
      || sudo python -m playwright install-deps chromium-headless-shell >/dev/null
    ffmpeg_tool ;;
  recap_shard)
    # Owner's own footage only (guarded by the client). Board frames + PiP composite of one block; everything that leaves is encrypted.
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    W="$RUNNER_TEMP/w"; O="$RUNNER_TEMP/out"; mkdir -p "$W" "$O" "$RUNNER_TEMP/enc"
    cat bundle.part_* | enc -d | tar xzf - -C "$W"          # board page, pip.py, timeline, face track, manifest (no video)
    cat footage.part_* | enc -d -out "$W/footage.bin"       # the speaker cut, uploaded on its own while the plan was made
    if ! FOOTAGE="$W/footage.bin" python3 farm/recap_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$O" > "$RUNNER_TEMP/shard.log" 2>&1; then
      tail -c 4000 "$RUNNER_TEMP/shard.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$SHARD").enc"
      rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log"; echo "shard failed"; exit 1
    fi
    echo "sub-ranges done: $(grep -c 'sub-range' "$RUNNER_TEMP/shard.log")"
    python3 -c "import json,glob; m=json.load(open(glob.glob('$O/meta_*.json')[0])); print({k: m.get(k) for k in ('lo','hi','board_frames','board_s','board_clip_s','pip_s','wa_s')})"
    tar cf - -C "$O" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
    rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log" ;;
  recap_join)
    # stream copy only: the final is pip.py's own encode (as the approved v2 was), the voice+music mix is the Mac's AAC
    [ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
    P="$RUNNER_TEMP/parts"; J="$RUNNER_TEMP/j"; F="$RUNNER_TEMP/final"; mkdir -p "$J" "$F"
    for f in "$P"/chunk_*.tar.enc; do enc -d -in "$f" | tar xf - -C "$J"; done
    enc -d -in audio.enc -out "$J/audio.m4a"
    cd "$J"
    SHARDS="$SHARDS" python3 -c '
import json, glob, os
m = sorted((json.load(open(f)) for f in glob.glob("meta_*.json")), key=lambda x: x["lo"])
assert len(m) == int(os.environ["SHARDS"]), "shard metas missing"
m = [x for x in m if x["hi"] > x["lo"]]
assert m[0]["lo"] == 0 and all(a["hi"] == b["lo"] for a, b in zip(m, m[1:])) and m[-1]["hi"] == m[0]["total"], "blocks do not tile the video"
open("expected", "w").write(str(m[0]["total"]))
open("list.txt", "w").write("".join("file %s\n" % p for x in m for p in x["pieces"]))
wa = [x["whatsapp"] for x in m]
open("wa.txt", "w").write("".join("file %s\n" % p for p in wa) if all(wa) else "")
print("pieces", sum(len(x["pieces"]) for x in m))
'
    ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -i audio.m4a -map 0:v -map 1:a -c copy -shortest -movflags +faststart final.mp4
    got=$(ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of default=nw=1:nk=1 final.mp4)
    exp=$(cat expected); echo "frames: got $got expected $exp"; [ "$got" = "$exp" ]
    enc -in final.mp4 -out "$F/final.mp4.enc"
    if [ -s wa.txt ]; then
      ffmpeg -y -loglevel error -f concat -safe 0 -i wa.txt -i audio.m4a -map 0:v -map 1:a -c:v copy -c:a aac -b:a 96k -shortest -movflags +faststart whatsapp.mp4
      enc -in whatsapp.mp4 -out "$F/whatsapp.mp4.enc"
    fi
    cd /; rm -rf "$J" "$P" ;;
  *) echo "unknown step"; exit 1 ;;
esac
