#!/usr/bin/env bash
# Download the third-party test videos used by the end-to-end tests into
# data/test_media/ (git-ignored). See data/test_media/ATTRIBUTION.md.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=data/test_media
mkdir -p "$DEST"
UA="ATCS-Monitor-test/1.0"

fetch() { [ -s "$DEST/$2" ] || curl -sSfL -A "$UA" -o "$DEST/$2" "$1"; echo "ok  $DEST/$2"; }

# Intel IoT DevKit sample videos - CC BY 4.0
for f in person-bicycle-car-detection.mp4 car-detection.mp4 people-detection.mp4; do
  fetch "https://github.com/intel-iot-devkit/sample-videos/raw/master/$f" "$f"
done
# Ultralytics CI demo clip (AGPL-3.0 assets repo)
fetch "https://github.com/ultralytics/assets/releases/download/v0.0.0/solutions_ci_demo.mp4" solutions_ci_demo.mp4

# Wikimedia Commons street scenes, transcoded to 1280x720 @ 15 fps H.264
street() {
  local url="$1" out="$2"
  [ -s "$DEST/$out" ] && { echo "ok  $DEST/$out"; return; }
  command -v ffmpeg >/dev/null || { echo "ffmpeg is required to transcode $out" >&2; return 1; }
  local tmp; tmp="$(mktemp -d "$DEST/.dl.XXXX")"
  curl -sSfL -A "$UA" -o "$tmp/src.webm" "$url"
  ffmpeg -v error -y -i "$tmp/src.webm" -an -vf scale=1280:720 -r 15 -c:v libx264 -preset veryfast -crf 23 \
    -pix_fmt yuv420p "$DEST/$out"
  rm -rf "$tmp"
  echo "ok  $DEST/$out"
}
street "https://upload.wikimedia.org/wikipedia/commons/1/16/Traffic_situation_in_Mango_Avenue_Cebu_City.webm" street_cebu.mp4
street "https://upload.wikimedia.org/wikipedia/commons/d/d5/Traffic_on_Slovenska_street.webm" street_slovenska.mp4
