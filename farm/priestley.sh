#!/usr/bin/env bash
# Priestley-style shard (7 Nov 2026): same steps as run.sh recap_shard, but the composite is farm/priestley_shard.py
# (rounded-rect PiP + creep). Logs stay neutral; everything that leaves is encrypted.
set -euo pipefail
RENDER_KEY="$(printf %s "${RENDER_KEY:-}" | tr -d '\r\n')"; export RENDER_KEY
enc() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:RENDER_KEY "$@"; }
[ -n "${RENDER_KEY:-}" ] || { echo "RENDER_KEY secret is not set"; exit 1; }
W="$RUNNER_TEMP/w"; O="$RUNNER_TEMP/out"; mkdir -p "$W" "$O" "$RUNNER_TEMP/enc"
cat bundle.part_* | enc -d | tar xzf - -C "$W"
cat footage.part_* | enc -d -out "$W/footage.bin"
if ! FOOTAGE="$W/footage.bin" python3 farm/priestley_shard.py "$W/manifest.json" "$SHARD" "$SHARDS" "$O" > "$RUNNER_TEMP/shard.log" 2>&1; then
  tail -c 4000 "$RUNNER_TEMP/shard.log" | enc -out "$RUNNER_TEMP/enc/err_$(printf %03d "$SHARD").enc"
  rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log"; echo "shard failed"; exit 1
fi
echo "sub-ranges done: $(grep -c 'sub-range' "$RUNNER_TEMP/shard.log" || true)"
tar cf - -C "$O" . | enc -out "$RUNNER_TEMP/enc/chunk_$(printf %03d "$SHARD").tar.enc"
rm -rf "$W" "$O" "$RUNNER_TEMP/shard.log"
