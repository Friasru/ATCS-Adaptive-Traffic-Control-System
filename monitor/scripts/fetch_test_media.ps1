# Windows version of fetch_test_media.sh: download the third-party test videos into data\test_media\
# (git-ignored). See docs\test_media_attribution.md.
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_test_media.ps1
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is very slow with the progress bar
$Root = Split-Path -Parent $PSScriptRoot
$Dest = Join-Path $Root "data\test_media"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$UA = "ATCS-Monitor-test/1.0"

function Fetch([string]$Url, [string]$Name) {
    $out = Join-Path $Dest $Name
    if (-not ((Test-Path $out) -and (Get-Item $out).Length -gt 0)) {
        Invoke-WebRequest -Uri $Url -OutFile $out -UserAgent $UA -UseBasicParsing
    }
    Write-Host "ok  $out"
}

# Intel IoT DevKit sample videos - CC BY 4.0
foreach ($f in @("person-bicycle-car-detection.mp4", "car-detection.mp4", "people-detection.mp4")) {
    Fetch "https://github.com/intel-iot-devkit/sample-videos/raw/master/$f" $f
}
# Ultralytics CI demo clip (AGPL-3.0 assets repo)
Fetch "https://github.com/ultralytics/assets/releases/download/v0.0.0/solutions_ci_demo.mp4" "solutions_ci_demo.mp4"

# Wikimedia Commons street scenes, transcoded to 1280x720 @ 15 fps.
# ffmpeg (H.264) gives files identical in content to the Linux script; without ffmpeg the
# project's OpenCV is used (mp4v), which decodes the same but may shift counts by a frame.
$Py = Join-Path $Root ".venv-win\Scripts\python.exe"
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
function Street([string]$Url, [string]$Name) {
    $out = Join-Path $Dest $Name
    if ((Test-Path $out) -and (Get-Item $out).Length -gt 0) { Write-Host "ok  $out"; return }
    $tmp = Join-Path $Dest (".dl." + [guid]::NewGuid().ToString("N").Substring(0, 6))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $src = Join-Path $tmp "src.webm"
        Invoke-WebRequest -Uri $Url -OutFile $src -UserAgent $UA -UseBasicParsing
        if ($ffmpeg) {
            & $ffmpeg.Source -v error -y -i $src -an -vf scale=1280:720 -r 15 -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p $out
        } elseif (Test-Path $Py) {
            & $Py -I -c @"
import sys, cv2
src, dst = sys.argv[1], sys.argv[2]
cap = cv2.VideoCapture(src)
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
w = cv2.VideoWriter(dst, cv2.VideoWriter_fourcc(*'mp4v'), 15.0, (1280, 720))
step, t, n = fps / 15.0, 0.0, 0
while True:
    ok, img = cap.read()
    if not ok:
        break
    while n >= t:  # resample to 15 fps
        w.write(cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA))
        t += step
    n += 1
w.release()
"@ $src $out
        } else {
            throw "need ffmpeg or .venv-win (run scripts\setup.ps1) to transcode $Name"
        }
        if ($LASTEXITCODE -ne 0) { throw "transcoding $Name failed" }
    } finally {
        Remove-Item -Recurse -Force $tmp
    }
    Write-Host "ok  $out"
}
Street "https://upload.wikimedia.org/wikipedia/commons/1/16/Traffic_situation_in_Mango_Avenue_Cebu_City.webm" "street_cebu.mp4"
Street "https://upload.wikimedia.org/wikipedia/commons/d/d5/Traffic_on_Slovenska_street.webm" "street_slovenska.mp4"
