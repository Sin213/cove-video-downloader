<#
.SYNOPSIS
    Build Cove Video Downloader into a Windows Setup installer and a
    single-file portable executable. Both bundle ffmpeg.exe and HandBrakeCLI.exe.
    yt-dlp is fetched on first launch and auto-updated thereafter.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Version 1.2.0
#>

[CmdletBinding()]
param(
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$App        = "cove-video-downloader"
$ReleaseDir = "release"
$FfmpegUrl  = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

function Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Download-File([string]$url, [string]$dest) {
    & curl.exe --silent --show-error --fail --location --output $dest $url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $url" }
}

Step "Building $App v$Version"

# --- 1. Build venv ----------------------------------------------------------
Step "[1/7] Creating build venv"
if (Test-Path .buildenv) { Remove-Item -Recurse -Force .buildenv }
python -m venv .buildenv
& .\.buildenv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.buildenv\Scripts\python.exe -m pip install --quiet pyinstaller pillow

# --- 2. Generate .ico -------------------------------------------------------
Step "[2/7] Generating cove_icon.ico"
& .\.buildenv\Scripts\python.exe -c @"
from PIL import Image
Image.open('cove_icon.png').save(
    'cove_icon.ico',
    sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)],
)
"@

# --- 3. Download ffmpeg -----------------------------------------------------
Step "[3/7] Downloading ffmpeg (yt-dlp FFmpeg-Builds)"
$ffTmp = Join-Path ([IO.Path]::GetTempPath()) ("ffmpeg-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $ffTmp | Out-Null
$ffZip = Join-Path $ffTmp "ffmpeg.zip"
Download-File $FfmpegUrl $ffZip
Expand-Archive -Path $ffZip -DestinationPath $ffTmp -Force
$ffmpegExe = Get-ChildItem -Path $ffTmp -Filter ffmpeg.exe -Recurse |
             Select-Object -First 1 -ExpandProperty FullName
if (-not $ffmpegExe) { throw "ffmpeg.exe missing from downloaded archive" }

# --- 4. HandBrake CLI via Chocolatey ----------------------------------------
Step "[4/7] Installing HandBrakeCLI via Chocolatey"
choco install handbrake-cli -y --no-progress | Out-Host
$hb = Get-ChildItem "C:\ProgramData\chocolatey\lib" -Recurse -Filter "HandBrakeCLI.exe" |
      Select-Object -First 1
if (-not $hb) { throw "HandBrakeCLI.exe not found in chocolatey\lib after install." }
$handbrakeExe = $hb.FullName

# --- 5. PyInstaller: one-dir (installer input) -------------------------------
Step "[5/7] PyInstaller (one-dir for installer)"
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist  }

$commonArgs = @(
    '--noconfirm', '--clean', '--log-level', 'WARN',
    '--windowed',
    '--name', $App,
    '--icon', 'cove_icon.ico',
    '--add-data', ("cove_icon.png" + [IO.Path]::PathSeparator + "."),
    '--add-binary', ($ffmpegExe + [IO.Path]::PathSeparator + '.'),
    '--add-binary', ($handbrakeExe + [IO.Path]::PathSeparator + '.'),
    'cove_video_downloader.py'
)

& .\.buildenv\Scripts\pyinstaller.exe @commonArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (onedir) failed" }

$dirAppDir = Join-Path 'dist' $App
Copy-Item cove_icon.png $dirAppDir -Force
if (Test-Path README.md) { Copy-Item README.md $dirAppDir -Force }
if (Test-Path LICENSE)   { Copy-Item LICENSE   $dirAppDir -Force }

# --- 6. PyInstaller: one-file (portable) -------------------------------------
Step "[6/7] PyInstaller (one-file portable)"
$portableName = "$App-portable"
& .\.buildenv\Scripts\pyinstaller.exe `
    --noconfirm --clean --log-level WARN `
    --onefile --windowed `
    --name $portableName `
    --icon cove_icon.ico `
    --add-data ("cove_icon.png" + [IO.Path]::PathSeparator + ".") `
    --add-binary ($ffmpegExe + [IO.Path]::PathSeparator + '.') `
    --add-binary ($handbrakeExe + [IO.Path]::PathSeparator + '.') `
    cove_video_downloader.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (onefile) failed" }

# --- 7. Installer + staging + cleanup ----------------------------------------
Step "[7/7] Building Setup installer with Inno Setup"
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

$iscc = $null
foreach ($candidate in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)) {
    if ($candidate -and (Test-Path $candidate)) { $iscc = $candidate; break }
}
if (-not $iscc) {
    $inPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($inPath) { $iscc = $inPath.Source }
}
if (-not $iscc) { throw "Inno Setup (iscc.exe) not found. Install Inno Setup 6." }

$absSource  = (Resolve-Path $dirAppDir).Path
$absRelease = (Resolve-Path $ReleaseDir).Path
$absIcon    = (Resolve-Path cove_icon.ico).Path

& $iscc `
    "/DAppVersion=$Version" `
    "/DSourceDir=$absSource" `
    "/DOutputDir=$absRelease" `
    "/DIconFile=$absIcon" `
    packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }

$portableSrc  = Join-Path 'dist' "$portableName.exe"
$portableDest = Join-Path $ReleaseDir ("{0}-{1}-Portable.exe" -f $App, $Version)
if (Test-Path $portableDest) { Remove-Item -Force $portableDest }
Copy-Item $portableSrc $portableDest -Force

Remove-Item -Recurse -Force .buildenv, build, dist, cove_icon.ico -ErrorAction SilentlyContinue
Get-ChildItem -Filter *.spec | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ffTmp -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done:" -ForegroundColor Green
Get-ChildItem $ReleaseDir -Filter "*$Version*" | ForEach-Object {
    Write-Host ("  {0}" -f $_.FullName)
}
