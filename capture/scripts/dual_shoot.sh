#!/usr/bin/env bash
set -euo pipefail

# --- Args ---
DISTANCE_CM="${1:?Usage: $0 <distance_cm> [outdir]}"
OUTDIR="${2:-captures}"
mkdir -p "$OUTDIR"

# --- Config ---
SIZES=("4624x3472" "3840x2160" "2312x1736")
TIMEOUT_MS=1500
EXTRA_OPTS=(--thumb none --buffer-count 2 --vflip --hflip --awb indoor --autofocus-on-capture)
CAM_STAGGER="0.02"

# --- Log setup ---
LOG="$OUTDIR/capture_log.csv"
if [[ ! -f "$LOG" ]]; then
  echo "timestamp_iso,run_id,distance_cm,width,height,camera,file_path,bytes,sha256,start_ms,end_ms,elapsed_ms,timeout_ms,exit_code,options,delta_cam1_minus_cam0_ms" > "$LOG"
fi

ms_now() { date +%s%3N; }
iso_now() { date -u +"%Y-%m-%dT%H:%M:%S.%3NZ"; }

# Compute SHA256 of a file
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    openssl dgst -sha256 "$1" | awk '{print $2}'
  fi
}

run_id="$(date +'%Y%m%d_%H%M%S_%3N')"

try_capture() {
  local size="$1"
  local W="${size%x*}" H="${size#*x}"

  local stamp="$run_id"
  local f0="$OUTDIR/${stamp}_${DISTANCE_CM}cm_${W}x${H}_cam0.jpg"
  local f1="$OUTDIR/${stamp}_${DISTANCE_CM}cm_${W}x${H}_cam1.jpg"

  echo "== Capturing ${W}x${H} at ${DISTANCE_CM} cm (run $run_id) =="

  # Start both captures in background
  local t0_start t1_start t0_end t1_end

  # Camera 0
  t0_start=$(ms_now)
  rpicam-still \
    --camera 0 -t "$TIMEOUT_MS" -n --width "$W" --height "$H" -o "$f0" \
    "${EXTRA_OPTS[@]}" &
  pid0=$!

  # Camera 1 (staggered start)
  sleep "$CAM_STAGGER"
  t1_start=$(ms_now)
  rpicam-still \
    --camera 1 -t "$TIMEOUT_MS" -n --width "$W" --height "$H" -o "$f1" \
    "${EXTRA_OPTS[@]}" &
  pid1=$!

  wait "$pid0"; ec0=$?; t0_end=$(ms_now)
  wait "$pid1"; ec1=$?; t1_end=$(ms_now)

  # Compute metrics
  local e0_ms=$(( t0_end - t0_start ))
  local e1_ms=$(( t1_end - t1_start ))
  local delta_start_ms=$(( t1_start - t0_start ))

  # Safe defaults if files didn’t materialize
  local b0=0 b1=0 s0="" s1=""
  [[ -f "$f0" ]] && b0=$(stat -c%s "$f0") && s0=$(sha256_of "$f0")
  [[ -f "$f1" ]] && b1=$(stat -c%s "$f1") && s1=$(sha256_of "$f1")

  local ts_iso="$(iso_now)"
  local opts_joined="${EXTRA_OPTS[*]}"

  # Logs
  echo "$ts_iso,$run_id,$DISTANCE_CM,$W,$H,cam0,$f0,$b0,$s0,$t0_start,$t0_end,$e0_ms,$TIMEOUT_MS,$ec0,\"$opts_joined\",$delta_start_ms" >> "$LOG"
  echo "$ts_iso,$run_id,$DISTANCE_CM,$W,$H,cam1,$f1,$b1,$s1,$t1_start,$t1_end,$e1_ms,$TIMEOUT_MS,$ec1,\"$opts_joined\",$delta_start_ms" >> "$LOG"

  echo "cam0: ${e0_ms} ms (exit $ec0) | ${b0} bytes"
  echo "cam1: ${e1_ms} ms (exit $ec1) | ${b1} bytes"
  echo "start delta cam1-cam0: ${delta_start_ms} ms"
}

# --- Main loop over requested sizes ---
for sz in "${SIZES[@]}"; do
  try_capture "$sz"
done

echo "Done. Log at: $LOG"
