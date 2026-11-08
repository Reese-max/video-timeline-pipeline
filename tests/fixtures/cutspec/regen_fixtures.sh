#!/bin/sh
# Regenerate frozen cutspec fixtures. Committed outputs are the canonical
# "frozen" revisions — rerun this only to intentionally roll the fixtures.
set -eu
cd "$(dirname "$0")"

for spec in "24000/1001 cfr_23976" "24/1 cfr_24" "25/1 cfr_25" "30000/1001 cfr_2997" "30/1 cfr_30"; do
  set -- $spec
  ffmpeg -v error -y \
    -f lavfi -i "testsrc=duration=4:size=64x64:rate=$1" \
    -c:v libx264 -preset ultrafast -crf 32 -pix_fmt yuv420p -an \
    "$2.mp4"
done

# VFR: drop every 5th frame, keep original timestamps (fps_mode vfr) so
# frame durations are non-uniform.
ffmpeg -v error -y \
  -f lavfi -i "testsrc=duration=4:size=64x64:rate=30" \
  -vf "select='not(eq(mod(n\,5)\,0))'" -fps_mode vfr \
  -c:v libx264 -preset ultrafast -crf 32 -pix_fmt yuv420p -an \
  vfr_30.mp4
